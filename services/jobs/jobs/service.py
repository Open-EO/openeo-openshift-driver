"""Provide the implementation of the main job management service and service exception."""
import json
import logging
import os
import random
import string
import threading
from collections import namedtuple
from datetime import datetime
from time import sleep
from typing import Any, Dict, List, Optional

from dynaconf import settings
from eodc_openeo_bindings.job_writer.dag_writer import AirflowDagWriter
from nameko.rpc import RpcProxy, rpc
from nameko_sqlalchemy import DatabaseSession

from .dependencies.airflow_conn import AirflowRestConnectionProvider
from .dependencies.dag_handler import DagHandlerProvider, DagIdExtensions
from .dependencies.settings import initialise_settings
from .exceptions import JobLocked, JobNotFinished, ServiceException
from .models import Base, Job, JobStatus
from .schema import JobCreateSchema, JobFullSchema, JobResultsBaseSchema, JobShortSchema

service_name = "jobs"
LOGGER = logging.getLogger('standardlog')
initialise_settings()


class JobService:
    """Management of batch processing tasks (jobs) and their results."""

    name = service_name
    db = DatabaseSession(Base)
    data_service = RpcProxy("data")
    """Database connection to jobs database."""
    processes_service = RpcProxy("processes")
    """Rpc connection to processes service."""
    files_service = RpcProxy("files")
    """Rpc connection to files service."""
    airflow = AirflowRestConnectionProvider()
    """Object to connection to Airflow REST endpoints."""
    dag_handler = DagHandlerProvider()
    dag_writer = AirflowDagWriter(DagIdExtensions().to_dict())
    """Object to write Airflow dags."""
    check_stop_interval = 5
    """Time interval in seconds to check whether a job was stopped.

    Should be similar or smaller than Airflow sensor's poke interval
    """

    @rpc
    def get(self, user: Dict[str, Any], job_id: str) -> dict:
        """Get all information about the job using the job_id.

        Args:
            user: The user object to determine whether the user has access to the given job.
            job_id: The id of the job to retrieve.

        Returns:
            A dictionary containing detailed information about the job and the request status or a serialized service
            exception.
        """
        try:
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()

            self._update_job_status(job_id=job_id)
            process_response = self.processes_service.get_user_defined(user, job.process_graph_id)
            if process_response["status"] == "error":
                return process_response
            job.process = process_response["data"]

            return {
                "status": "success",
                "code": 200,
                "data": JobFullSchema().dump(job)
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def modify(self, user: Dict[str, Any], job_id: str, **job_args: Any) -> dict:
        """Modify the job with the given job_id.

        Args:
            user: The user object to determine access rights.
            job_id: The id of the job to modify.
            job_args: A dictionary of new job arguments - where key: argument name / value: new value.

        Returns:
            A dictionary with the status of the request.
        """
        try:
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()

            self._update_job_status(job_id=job_id)
            if job.status in [JobStatus.queued, JobStatus.running]:
                return JobLocked(400, user["id"], f"Job {job_id} is currently {job.status} and cannot be modified",
                                 links=[]).to_dict()

            if job_args.get("process", None):

                # handle processes db
                process_graph_args = job_args.pop('process')
                process_graph_id = process_graph_args["id"] if "id" in process_graph_args \
                    else self.generate_alphanumeric_id()
                process_response = self.processes_service.put_user_defined(
                    user=user, process_graph_id=process_graph_id, **process_graph_args)
                if process_response["status"] == "error":
                    return process_response
                job.process_graph_id = process_graph_id

            # Maybe there is a better option to do this update? e.g. using marshmallow schemas?
            job.title = job_args.get("title", job.title)
            job.description = job_args.get("description", job.description)
            job.plan = job_args.get("plan", job.plan)
            if job_args.get("budget", None):
                job.budget = int(job_args["budget"] * 100)
            self.db.commit()

            return {
                "status": "success",
                "code": 204
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp),
                                    links=[]).to_dict()

    @rpc
    def delete(self, user: Dict[str, Any], job_id: str, delayed: bool = False) -> dict:
        """Completely delete the job with the given job_id.

        This will stop the job if it is currently queued or running, remove the job itself and all results.

        Args:
            user: The user object, to determine access rights.
            job_id: The id of the job.
            delayed: Whether this should happen directly or it should be delayed for some seconds. This can be used for
                synchronous where results need to be returned and then deleted.

        Returns:
            A dictionary with the status of the request.
        """
        # TODO handle costs (stop it)
        try:
            LOGGER.debug(f"Start deleting job {job_id}")
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()

            self._update_job_status(job_id=job_id)
            if job.status in [JobStatus.running, JobStatus.queued]:
                LOGGER.debug(f"Stopping running job {job_id}")
                self._stop_airflow_job(user["id"], job_id)
                LOGGER.info(f"Stopped running job {job_id}.")

            if delayed:
                # Schedule async deletion of tmp folder
                threading.Thread(target=self._delayed_delete, args=(user["id"], job_id)).start()
            else:
                self.files_service.delete_complete_job(user_id=user["id"], job_id=job_id)  # delete data on file system
            self.dag_handler.remove_all_dags(job_id)  # delete dag file
            for dag_id in self.dag_handler.get_all_dag_ids(job_id=job_id):
                self.airflow.delete_dag(dag_id=dag_id)  # delete from airflow database
            self.db.delete(job)  # delete from our job database
            self.db.commit()
            LOGGER.info(f"Job {job_id} completely deleted.")

            return {
                "status": "success",
                "code": 204
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def get_all(self, user: Dict[str, Any]) -> dict:
        """Get general information about all available jobs of a given user.

        Args:
            user: The user object.

        Returns:
            A dictionary including all available jobs and the status of the request or a serialized exception.
        """
        try:
            jobs = self.db.query(Job.id).filter_by(user_id=user["id"]).order_by(Job.created_at).all()
            for job in jobs:
                self._update_job_status(job.id)

            jobs = self.db.query(Job).filter_by(user_id=user["id"]).order_by(Job.created_at).all()
            return {
                "status": "success",
                "code": 200,
                "data": {
                    "jobs": JobShortSchema(many=True).dump(jobs),
                    "links": []
                }
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp),
                                    links=["#tag/Job-Management/paths/~1jobs/get"]).to_dict()

    @rpc
    def create(self, user: Dict[str, Any], **job_args: Any) -> dict:
        """Create a new job using the provided description (job_args).

        Args:
            user: The user who wants to add the new job.
            job_args: Details about the job as dictionary - e.g. the process graph.

        Returns:
            A dictionary with the id of the newly created job and the status of the request or a serialized service
            exception.
        """
        try:
            LOGGER.debug("Start creating job...")
            process = job_args.pop("process")
            process_graph_id = process["id"] if "id" in process else self.generate_alphanumeric_id()
            process_response = self.processes_service.put_user_defined(
                user=user, process_graph_id=process_graph_id, **process)
            if process_response["status"] == "error":
                return process_response
            LOGGER.info(f"ProcessGraph {process_graph_id} created")

            job_args["process_graph_id"] = process_graph_id
            job_args["user_id"] = user["id"]
            job = JobCreateSchema().load(job_args)
            self.db.add(job)
            self.db.commit()
            job_id = str(job.id)

            self.db.add(job)
            self.db.commit()
            LOGGER.info(f"Added job to database with id: {job_id}")

            return {
                "status": "success",
                "code": 201,
                "headers": {
                    "Location": "jobs/" + job_id,
                    "OpenEO-Identifier": job_id
                }
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp),
                                    links=["#tag/Job-Management/paths/~1jobs/post"]).to_dict()

    @rpc
    def process(self, user: Dict[str, Any], job_id: str) -> dict:
        """Start the processing of the given job.

        The job needs to exist on the backend and must not already be queued or running.

        Args:
            user: The user who wants to start the job, also has to own the job.
            job_id: The id of the job which should be started.

        Returns:
            A dictionary with the status of the request.
        """
        try:
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()

            self._update_job_status(job_id=job_id)
            if job.status in [JobStatus.queued, JobStatus.running]:
                return ServiceException(400, user["id"],
                                        f"Job {job_id} is already {job.status}. Processing must be canceled before"
                                        f" restart.", links=[], internal=False).to_dict()

            self.files_service.setup_jobs_result_folder(user_id=user["id"], job_id=job_id)

            # Get all processes
            process_response = self.processes_service.get_all_predefined()
            if process_response["status"] == "error":
                return process_response
            backend_processes = process_response["data"]["processes"]

            # Get process graph
            process_graph_response = self.processes_service.get_user_defined(user, job.process_graph_id)
            if process_graph_response["status"] == "error":
                return process_graph_response
            process_graph = process_graph_response["data"]["process_graph"]

            # Get input filepaths
            in_filepaths = self._get_in_filepaths(process_graph)
            if 'status' in in_filepaths and in_filepaths['status'] == 'error':
                # return data exception now stored in in_filepaths
                return in_filepaths

            self.dag_writer.write_and_move_job(
                job_id=job_id,
                user_name=user["id"],
                dags_folder=settings.AIRFLOW_DAGS,
                wekeo_storage=settings.WEKEO_STORAGE,
                process_graph_json={"process_graph": process_graph},
                job_data=self.get_latest_job_folder(user['id'], job_id),
                vrt_only=job.vrt_flag,
                add_delete_sensor=True,
                add_parallel_sensor=job.add_parallel_sensor,
                process_defs=backend_processes,
                in_filepaths=in_filepaths,
            )
            LOGGER.info(f"Dag file created for job {job_id}")

            trigger_worked = self.airflow.trigger_dag(dag_id=self.dag_handler.get_preparation_dag_id(job_id))
            if not trigger_worked:
                return ServiceException(500, user["id"], f"Job {job_id} could not be started.", links=[]).to_dict()

            self._update_job_status(job_id=job_id)
            LOGGER.info(f"Processing successfully started for job {job_id}")

            self.files_service.delete_old_job_runs(user["id"], job_id)

            return {
                "status": "success",
                "code": 202,
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def process_sync(self, user: Dict[str, Any], **job_args: Any) -> dict:
        """Execute a provided job synchronously.

        This method MUST ONLY be used for SMALL jobs!
        It creates a job from the provided job_args, starts it, waits until it is finished and returns the location of
        the resulting file.

        Currently the 'size' of the job is not check - needs to be improved in the future!

        Args:
            user: The user who processes the job.
            job_args: Details about the job including e.g. the process graph.

        Returns:
            A dictionary containing the status of the request and the filepath to the output of the job. If an error
            occurs a serialized service exception is returned.
        """
        TypeMap = namedtuple('TypeMap', 'file_extension content_type')
        type_map = {
            'Gtiff': TypeMap('tif', 'image/tiff'),
            'png': TypeMap('png', 'image/png'),
            'jpeg': TypeMap('jpeg', 'image/jpeg'),
        }

        try:
            # TODO: implement a check that the job qualifies for sync-processing
            # it has to be a "small" job, e.g. constriants for timespan and bboux, but also on spatial resolution

            # Create Job
            LOGGER.info("Creating job for sync processing.")
            job_args['vrt_flag'] = False
            job_args['add_parallel_sensor'] = False
            response_create = self.create(user=user, **job_args)
            if response_create['status'] == 'error':
                return response_create

            # Start processing
            job_id = response_create["headers"]["Location"].split('/')[-1]
            job = self.db.query(Job).filter_by(id=job_id).first()
            response_process = self.process(user=user, job_id=job_id)
            if response_process['status'] == 'error':
                return response_process

            LOGGER.info(f"Job {job_id} is running.")
            self._update_job_status(job_id=job_id)
            while job.status in [JobStatus.queued, JobStatus.running]:
                sleep(10)
                self._update_job_status(job_id=job_id)
            if job.status in [JobStatus.error, JobStatus.canceled]:
                msg = f"Job {job_id} has status: {job.status}."
                return ServiceException(400, user["id"], msg, links=[]).to_dict()

            LOGGER.info(f"Job {job_id} has been processed.")
            # just to hide from view on default Airflow web view
            self.airflow.unpause_dag(dag_id=self.dag_handler.get_non_parallel_dag_id(job_id), unpause=False)
            response_files = self.files_service.get_job_output(user_id=user["id"], job_id=job_id, internal=True)
            if response_files["status"] == "error":
                LOGGER.info(f"Could not retrieve output of Job {job_id}.")
                return response_files

            filepath = response_files['data']['file_list'][0]
            fmt = self.map_output_format(filepath.split('.')[-1])

            # Remove job data (sync jobs must not be stored)
            self.delete(user, job_id, delayed=True)

            return {
                "status": "success",
                "code": 200,
                "headers": {
                    "Content-Type": type_map[fmt].content_type,
                    "OpenEO-Costs": 0,
                },
                "file": filepath
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def estimate(self, user: Dict[str, Any], job_id: str) -> dict:
        """Return a cost estimation for a given job - currently a default value of 0 is returned.

        Args:
            user: The user object, to determine access rights.
            job_id: The id of the job to check.

        Returns:
            A dictionary including the status of the request and estimated costs or a serialized service exception.
        """
        default_out = {
            "costs": 0,
        }

        LOGGER.info("Costs estimated.")
        return {
            "status": "success",
            "code": 200,
            "data": default_out
        }

    @rpc
    def cancel_processing(self, user: Dict[str, Any], job_id: str) -> dict:
        """Cancel the processing of the given job.

        This will stop the processing if the job is currently queued or running and remove all not persistent result.
        The job definition and already processed results are kept.

        Args:
            user: The user object to determine access rights.
            job_id: The id of the job which should be canceled.

        Returns:
            The status of the request.
        """
        try:
            # TODO handle costs (stop it)
            LOGGER.debug(f"Start canceling job {job_id}")
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()

            self._update_job_status(job_id=job_id)
            if job.status in [JobStatus.running, JobStatus.queued]:
                LOGGER.debug(f"Stopping running job {job_id}...")
                self._stop_airflow_job(user["id"], job_id)
                LOGGER.info(f"Stopped running job {job_id}.")

                results_exists = self.files_service.delete_job_without_results(user["id"], job_id)
                if job.status == JobStatus.running and results_exists:
                    self._set_job_status(job_id, JobStatus.canceled)
                else:
                    self._set_job_status(job_id, JobStatus.created)
                self.db.commit()
                LOGGER.info(f"Job {job_id} has not the status {job.status}.")

            LOGGER.info(f"Job {job_id} canceled.")
            return {
                "status": "success",
                "code": 204
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp),
                                    links=["#tag/Job-Management/paths/~1jobs~1{job_id}~1results/delete"]).to_dict()

    @rpc
    def get_results(self, user: Dict[str, Any], job_id: str, api_spec: dict) -> dict:
        """Get the location (filepath) where the results of the given job can be retrieved.

        This only works if the job is in state 'finished'.

        Args:
            user: The user object.
            job_id: The id of the job.
            api_spec: OpenAPI Specification (needed for STAC Version).

        Returns:
            A dictionary containing the some metadata about the job, filepaths to result files and the status of the
            request. In case an error occurs a serialized service exception is returned.
        """
        try:
            job = self.db.query(Job).filter_by(id=job_id).first()
            response = self.authorize(user["id"], job_id, job)
            if isinstance(response, ServiceException):
                return response.to_dict()
            self._update_job_status(job_id=job_id)
            job = self.db.query(Job).filter_by(id=job_id).first()

            if job.status == JobStatus.error:
                return ServiceException(424, user["id"], job.error, internal=False).to_dict()  # TODO store error!

            if job.status == JobStatus.canceled:
                return ServiceException(400, user["id"], f"Job {job_id} was canceled.", internal=False).to_dict()

            if job.status in [JobStatus.created, JobStatus.queued, JobStatus.running]:
                return JobNotFinished(400, user["id"], job_id, internal=False).to_dict()

            # Job status is "finished"
            output = self.files_service.get_job_output(user_id=user["id"], job_id=job_id)
            if output["status"] == "error":
                return output
            file_list = output["data"]["file_list"]
            metadata_file = output["data"]["metadata_file"]

            # # Add additional metadata from json
            with open(metadata_file) as f:
                metadata = json.load(f)

            job.assets = [{
                "href": self._get_download_url(api_spec["servers"][0]["url"], f),
                "name": os.path.basename(f)
            } for f in file_list]

            # TODO fix links
            job.links = [{"href": "https://openeo.eodc.eu/v1.0/collections", "rel": "self"}]
            job.stac_version = api_spec["info"]["stac_version"]

            # file list could be retrieved
            job_data = JobResultsBaseSchema().dump(job)
            job_data.update(metadata)
            # Fix 'type' field, must always be 'Feature'
            if job_data['type'] != "Feature":
                job_data['type'] = "Feature"

            return {
                "status": "success",
                "code": 200,
                "headers": {
                    "Expires": "not given",
                    "OpenEO-Costs": 0
                },
                "data": job_data,
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    def _get_download_url(self, base_url: str, public_path: str) -> str:
        """Create the download url from the public filepath of a result file.

        Args:
            base_url: The base URI visible from the outside e.g. https://openeo.eodc.eu or http://localhost:3000.
            public_path: A public filepath (NOT the complete path on the file system!).

        Returns:
            Complete url from where the file can be downloaded.
        """
        return os.path.join(base_url, settings.OPENEO_VERSION, "downloads", public_path)

    @staticmethod
    def authorize(user_id: str, job_id: str, job: Optional[Job]) -> Optional[ServiceException]:
        """Return Exception if given Job does not exist or User is not allowed to access this Job.

        Arguments:
            user_id: The identifier of the user.
            job_id: The id of the job.
            job: The Job object for the given job_id.
        """
        if job is None:
            return ServiceException(400, user_id, f"The job with id '{job_id}' does not exist.",
                                    internal=False, links=["#tag/Job-Management/paths/~1data/get"])

        # TODO: Permission (e.g admin)
        if job.user_id != user_id:
            return ServiceException(401, user_id, f"You are not allowed to access the job {job_id}.",
                                    internal=False, links=["#tag/Job-Management/paths/~1data/get"])

        LOGGER.info(f"User is authorized to access job {job_id}.")
        return None

    def _set_job_status(self, job_id: str, new_status: JobStatus) -> None:
        job = self.db.query(Job).filter_by(id=job_id).first()
        job.status = new_status
        job.status_updated_at = datetime.utcnow()
        self.db.commit()
        LOGGER.debug(f"Job Status of job {job_id} is {job.status}")

    def _update_job_status(self, job_id: str) -> None:
        """Update the job status.

        Whenever the job status is updated this method should be used to ensure the status_updated_at column is properly
        set! The new status is retrieved from airflow.

        One job creates two dags, one to create the processing instructions as vrt files and one which executes the
        commands in parallel. Therefore always the status of the "newer" dag is used.

        Args:
            job_id: The id of the job.
        """
        job = self.db.query(Job).filter_by(id=job_id).first()
        dag_ids = self.dag_handler.get_all_dag_ids(job_id)
        all_status = []
        all_execution_time = []
        for dag_id in dag_ids:
            new_status, execution_time = self.airflow.check_dag_status(dag_id=dag_id)
            if new_status and (not job.status
                               or job.status in [JobStatus.created,
                                                 JobStatus.queued,
                                                 JobStatus.running,
                                                 JobStatus.error]
                               # Job status created or canceled, when job is canceled:
                               # > state in airflow set to failed though locally is stored as created or canceled
                               # > the state should only be updated if there was a new dag run since the canceled one
                               # - all times are stored in UTC
                               or (execution_time and job.status_updated_at.replace(tzinfo=None) < execution_time)):
                all_status.append(new_status)
                all_execution_time.append(execution_time)

        if all_status:
            # both equal or one was not rerun after cancel > only one value in list
            if all(status == all_status[0] for status in all_status):
                job.status = all_status[0]
            else:
                # execution time should always be set except when created is returned > both created > above case
                idx = all_execution_time.index(max(all_execution_time))
                job.status = all_status[idx]

            job.status_updated_at = datetime.utcnow()
            self.db.commit()
        LOGGER.debug(f"Job Status of job {job_id} is {job.status}")

    def get_latest_job_folder(self, user_id: str, job_id: str) -> str:
        """Get absolute path to latest job_run folder of a user.

        Args:
            user_id: The identifier of the user.
            job_id: The id of the job.

        Returns:
            The complete path to the specific job folder on the file system.
        """
        latest_job_run = self.files_service.get_latest_job_run_folder_name(user_id, job_id)
        return os.path.join(settings.AIRFLOW_OUTPUT, user_id, "jobs", job_id, latest_job_run)

    def _get_old_job_folders(self, user_id: str, job_id: str) -> List[str]:
        """Get absolute path to all job_runs but the latest one."""
        old_job_runs = self.files_service.get_old_job_run_folder_names()
        return [os.path.join(settings.AIRFLOW_OUTPUT, user_id, "jobs", job_id, job_run) for job_run in old_job_runs]

    def _stop_airflow_job(self, user_id: str, job_id: str) -> None:
        """Trigger the airflow observer to set all running task to failed.

        This will stop any successor tasks to start but it will not stop the currently running task.

        Args:
            user_id: The identifier of the user.
            job_id: The id of the job.
        """
        self.files_service.upload_stop_job_file(user_id, job_id)

        # Wait till job is stopped
        job_stopped = False
        while not job_stopped:
            LOGGER.info("Waiting for airflow sensor to detect STOP file...")
            sleep(self.check_stop_interval)
            job_stopped = self.airflow.check_dag_status(job_id) != JobStatus.running

    @staticmethod
    def map_output_format(output_format: str) -> str:
        """Map synonyms to a defined output format."""
        out_map = [(['Gtiff', 'GTiff', 'tif', 'tiff'], 'Gtiff'),
                   (['jpg', 'jpeg'], 'jpeg'),
                   (['png'], 'png')
                   ]
        for synonyms, out_name in out_map:
            if output_format in synonyms:
                return out_name
        raise ValueError('{} is not a supported output format'.format(output_format))

    def _delayed_delete(self, user: Dict[str, Any], job_id: str) -> None:
        """Wait for some time and then delete a complete folder structure corresponding to a job.

        This is used for the sync processing to ensure result data is downloaded before it is deleted.

        Args:
            folder_path {str} -- The full path to the folder to be deleted
        """
        # Wait n minutes (allow for enough time to stream file(s) to user)
        sleep(settings.SYNC_DEL_DELAY)
        # Delete data on file system
        self.files_service.delete_complete_job(user_id=user["id"], job_id=job_id)
        LOGGER.info(f"Deleted data on filesystem for job_id:{job_id}.")

    def generate_alphanumeric_id(self, k: int = 16) -> str:
        """Generate a random alpha numeric value."""
        return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

    def _get_in_filepaths(self, process_graph: dict) -> dict:
        """Return filepaths for current process_graph.

        Generate a dictionary storing in_filepaths, one for any load_collection
        call in the current process graph.

        Arguments:
            process_graph {dict} -- an openEO process graph

        Returns:
            in_filepaths -- dict storing lists of in_filepaths, one for each load_collection node
                         -- OR dict with data_response if the request returns an error
        """
        in_filepaths: dict = {}
        for node in process_graph:
            if process_graph[node]['process_id'] == 'load_collection':
                in_filepaths[node] = {}
                collection_id = process_graph[node]['arguments']['id']

                spatial_extent = [
                    process_graph[node]['arguments']['spatial_extent']['south'],
                    process_graph[node]['arguments']['spatial_extent']['east'],
                    process_graph[node]['arguments']['spatial_extent']['north'],
                    process_graph[node]['arguments']['spatial_extent']['west']
                ]

                temporal_extent = process_graph[node]['arguments']['temporal_extent']

                data_response = self.data_service.get_filepaths(collection_id, spatial_extent, temporal_extent)
                if data_response["status"] == "error":
                    return data_response
                in_filepaths[node] = data_response["data"]

        return in_filepaths
