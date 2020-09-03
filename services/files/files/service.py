"""Provides the implementation of the file management service and service exception."""

import glob
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from dynaconf import settings
from nameko.rpc import rpc
from werkzeug.security import safe_join

from .dependencies.settings import initialise_settings

service_name = "files"
LOGGER = logging.getLogger('standardlog')
initialise_settings()


class ServiceException(Exception):
    """ServiceException is raised if an exception occurred while processing the request.

    The ServiceException is mapping any exception to a serializable format for the API gateway.
    Attributes:
        code: An integer holding the error code.
        user_id: The id of the user as string.
        msg: A string with the error message.
        internal: A boolean indicating if this is an internal error. (default: True)
        links: A list of links which can be useful when getting this error. (default: None)
    """

    def __init__(self, code: int, user_id: str, msg: str, internal: bool = True, links: list = None) -> None:
        """Initialize file service ServiceException."""
        if not links:
            links = []

        self._service = service_name
        self._code = code
        self._user_id = user_id
        self._msg = msg
        self._internal = internal
        self._links = links
        LOGGER.exception(msg, exc_info=True)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the object to a dict.

        Returns:
            The serialized exception.
        """
        return {
            "status": "error",
            "service": self._service,
            "code": self._code,
            "user_id": self._user_id,
            "msg": self._msg,
            "internal": self._internal,
            "links": self._links,
        }


class FileOperationUnsupported(ServiceException):
    """FileOperationUnsupported raised if folder is passed when file is expected."""

    def __init__(self, code: int, user_id: str, msg: str, internal: bool = True, links: list = None) -> None:
        """Initialize FileOperationUnsupported exception."""
        super(FileOperationUnsupported, self).__init__(code, user_id, msg, internal, links)


class FilesService:
    """Management of files created by a user."""

    name = service_name

    # each directory / file name is only allowed to use a max. of 200 Alpha-Numeric characters
    allowed_dirname = re.compile(r'[a-zA-Z0-9_-]{1,200}')
    """Directory name restriction in regex format.

    Each directory is only allowed to consist of max. 200 alpha-numeric characters.
    """
    allowed_filename = re.compile(r'[a-zA-Z0-9_-]{1,200}\.[a-zA-Z0-9]{1,10}')
    """Filename name restriction in regex format.

    Each filename is only allowed to consist of max. 200 alpha-numeric characters with at top another 10 alpha-numeric
    characters as file extension."""

    files_folder = "files"
    """Name of the folder where all user uploads are stored - folder exists inside each user folder."""
    jobs_folder = "jobs"
    """Name of the folder where all jobs computed by a user are stored - folder exists inside each user folder."""
    result_folder = "result"
    """Name of the folder where job results computed by a user are stored - folder exists per single computed job."""

    @rpc
    def download(self, user: Dict[str, Any], path: str, source_dir: str = None) -> dict:
        """Return the file stored at the given path.

        Args:
            user: User determines if file is accessible.
            path: The file path to the requested file.
            source_dir: Top level folder name inside user folder. This can either be 'files' or 'jobs' in the current
                setup. Defaults to the files directory ('files').

        Returns:
            A dictionary with the complete path on the file system or a serialized exception.
        """
        try:
            source_dir = source_dir if source_dir else self.files_folder
            response = self.authorize_existing_file(user["id"], path, source_dir=source_dir)
            if isinstance(response, ServiceException):
                return response.to_dict()

            LOGGER.info(f"Download file {path}")
            return {
                "status": "success",
                "code": 200,
                "headers": {"Content-Type": "application/octet-stream"},
                "file": response,
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def delete(self, user: Dict[str, Any], path: str) -> dict:
        """Delete the file at the given path.

        Args:
            user: The user object determines whether the given path is accessible.
            path: The location of the file.

        Returns:
            The status (success/failure) of the delete operation as dictionary.
        """
        try:
            response = self.authorize_existing_file(user["id"], path)
            if isinstance(response, ServiceException):
                return response.to_dict()

            os.remove(response)
            LOGGER.info(f"File {path} successfully deleted.")
            return {
                "status": "success",
                "code": 204
            }
        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def get_all(self, user: Dict[str, Any]) -> dict:
        """Get all available files for the given user.

        Args:
            user: The user object to get list of files for.

        Returns:
            A sorted list of files available in the user's files folder or a serilized service exception. (No computed
            but uploaded data only!)
        """
        try:
            prefix, _ = self.setup_user_folder(user["id"])
            file_list = []

            for root, _, files in os.walk(prefix):
                user_root = root[len(prefix) + 1:]
                for f in files:
                    public_filepath = os.path.join(user_root, f)
                    internal_filepath = os.path.join(root, f)
                    file_list.append(
                        {
                            "path": public_filepath,
                            "size": int(os.path.getsize(internal_filepath)),
                            "modified": self.get_file_modification_time(internal_filepath)
                        }
                    )
            LOGGER.info(f"Found {len(file_list)} files in workspace of User {user['id']}.")
            return {
                "status": "success",
                "code": 200,
                "data": {
                    "files": sorted(file_list, key=lambda file: file['path']),
                    "links": [],
                }
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def upload(self, user: Dict[str, Any], path: str, tmp_path: str) -> dict:
        """Create a new job using the description send in the request body.

        Args:
            user: User who uploads a file.
            path: The destination path of the file.
            tmp_path: The path where the file is temporary stored.

        Returns:
            Some statistics about the file or a serialized service exception.
        """
        try:
            response = self.authorize_file(user["id"], path)
            if isinstance(response, ServiceException):
                os.remove(tmp_path)
                return response.to_dict()

            complete_path = response
            dirs, filename = os.path.split(complete_path)
            if not os.path.exists(dirs):
                os.makedirs(dirs, mode=0o700)

            os.rename(tmp_path, complete_path)
            LOGGER.info(f"File {path} successfully uploaded to User {user['id']} workspace.")
            return {
                "status": "success",
                "code": 200,
                "data": {
                    "path": self.complete_to_public_path(user["id"], complete_path),
                    "size": int(os.path.getsize(complete_path)),
                    "modified": self.get_file_modification_time(complete_path)
                }
            }

        except Exception as exp:
            return ServiceException(500, user["id"], str(exp), links=[]).to_dict()

    @rpc
    def setup_user_folder(self, user_id: str) -> List[str]:
        """Create user folder structure and return the paths.

        This creates a folder named <user_id> with two folders inside: 'files' and 'jobs'.

        Args:
            user_id: The identifier of the user.

        Returns:
            Two absolute paths - the first one points to the 'files' the second to the 'jobs' directory.
        """
        user_dir = self.get_user_folder(user_id)
        dirs_to_create = [os.path.join(user_dir, dir_name) for dir_name in [self.files_folder, self.jobs_folder]]

        for d in dirs_to_create:
            if not os.path.exists(d):
                LOGGER.info(f"Folder {d} successfully created")
                os.makedirs(d)

        LOGGER.info(f"User folder successfully setup for User {user_id}.")
        return dirs_to_create

    @staticmethod
    def get_user_folder(user_id: str) -> str:
        """Get path to user folder from user_id."""
        return os.path.join(settings.OPENEO_FILES_DIR, user_id)

    def authorize_file(self, user_id: str, path: str, source_dir: str = None) \
            -> Union[ServiceException, str]:
        """Return Exception if path is invalid or points to a directory.

        Args:
            user_id: The identifier of the user.
            path: The file path to the requested file.
            source_dir: Top level folder name inside user folder. This can either be 'files' or 'jobs' in the current
                setup. Defaults to the files directory ('files').

        Returns:
            If authorized the complete path on the file system is returned otherwise an error is returned.
        """
        source_dir = source_dir if source_dir else self.files_folder

        # check pattern
        complete_path = self.get_allowed_path(user_id, path.split('/'), source_dir=source_dir)
        if not complete_path:
            return FileOperationUnsupported(401, user_id, f"{path}: This path is not valid.", internal=False, links=[])

        if os.path.isdir(complete_path):
            return FileOperationUnsupported(400, user_id, f"{path}: Must be a file, no directory.", internal=False,
                                            links=[])
        LOGGER.info(f'User {user_id} is granted access to {path}')
        return complete_path

    def authorize_existing_file(self, user_id: str, path: str, source_dir: str = 'files') \
            -> Union[ServiceException, str]:
        """Return Exception if path is invalid, points to a directory or DOES NOT EXIST.

        Args:
            user_id: The identifier of the user.
            path: The file path to the requested file.
            source_dir: Top level folder name inside user folder. This can either be 'files' or 'jobs' in the current
                setup. Defaults to the files directory ('files').

        Returns:
            If authorized the complete path on the file system is returned otherwise an error is returned.

        """
        response = self.authorize_file(user_id, path, source_dir=source_dir)
        if isinstance(response, str) and not os.path.exists(response):
            return FileOperationUnsupported(404, user_id, f"{path}: No such file or directory.", internal=False,
                                            links=[])
        LOGGER.info(f"File {path} exists.")
        return response

    def get_allowed_path(self, user_id: str, parts: List[str], source_dir: str = 'files') -> Optional[str]:
        """Check if file name matches allowed pattern.

        Args:
            user_id: The identifier of the user.
            parts: List of all directory names and the filename.
            source_dir: Top level folder name inside user folder. This can either be 'files' or 'jobs' in the current
                setup. Defaults to the files directory ('files').

        Returns:
            The file path if it is allowed otherwise None.
        """
        files_dir, jobs_dir = self.setup_user_folder(user_id)
        if source_dir == 'files':
            out_dir = files_dir
        else:
            out_dir = jobs_dir

        filename = parts.pop(-1)
        for part in parts:
            if re.fullmatch(self.allowed_dirname, part) is None:
                return None
        if re.fullmatch(self.allowed_filename, filename) is None:
            return None
        parts.append(filename)

        return safe_join(out_dir, *parts)

    def complete_to_public_path(self, user_id: str, complete_path: str) -> str:
        """Create the public path seen by the user from a path on the file system.

        When a user uploads a file e.g. to my_folder/file1.json the file will be stored for instance at
        /path/to/folder/<user_id>/<files_folder>/my_folder/file1.json. This function can then be used to map the real
        path on the file system back to the path the user would expect.

        Args:
            user_id: The identifier of the user.
            complete_path: A complete file path on the file system.

        Returns:
            The corresponding public path to the file visible to the user.
        """
        return complete_path.replace(f'{self.get_user_folder(user_id)}/{self.files_folder}/', '')

    def get_file_modification_time(self, filepath: str) -> str:
        """Return timestamp of last modification in format: '2019-05-21T16:11:37Z'."""
        numeric_tstamp = os.path.getmtime(filepath)
        timestamp = datetime.fromtimestamp(numeric_tstamp).isoformat("T", "seconds") + "Z"

        return timestamp

    # needed for job management
    @rpc
    def setup_jobs_result_folder(self, user_id: str, job_id: str) -> str:
        """Create user folder structure with folder for the given job_id.

        Args:
            user_id: The identifier of the user.
            job_id: The identifier for the job.

        Returns:
            Path to the job results folder for the given user and job id.
        """
        self.setup_user_folder(user_id)
        to_create = self.get_job_results_folder(user_id, job_id)
        if not os.path.exists(to_create):
            LOGGER.debug(f"Creating Job results folder {to_create}.")
            os.makedirs(to_create)
        LOGGER.info(f"Job results folder {to_create} exists.")
        return to_create

    @rpc
    def get_job_output(self, user_id: str, job_id: str) -> dict:
        """Return the list of output files produced by a job.

        Args:
            user_id: The identifier of the user.
            job_id: The identifier of the job.

        Returns:
            A list of output files produced by the given job or a serialized service exception.
        """
        try:
            file_list = glob.glob(os.path.join(self.get_job_results_folder(user_id, job_id), '*'))
            if not file_list:
                return ServiceException(400, user_id, "Job output folder is empty. No files generated.").to_dict()

            LOGGER.info(f"Found {len(file_list)} output files for job {job_id}.")
            return {
                "status": "success",
                "code": 200,
                "data": {"file_list": [self.complete_to_public_path(user_id, f) for f in file_list]}
            }

        except Exception as exp:
            return ServiceException(500, user_id, str(exp)).to_dict()

    @rpc
    def download_result(self, user: Dict[str, Any], path: str) -> dict:
        """Get the job result files stored at the given path.

        Arguments:
            user: The user object who computed the job.
            path: The file path to the requested file.

        Returns:
            A dictionary with the complete path on the file system or a serialized exception.
        """
        return self.download(user, path, source_dir='jobs')

    @rpc
    def upload_stop_job_file(self, user_id: str, job_id: str) -> None:
        """Create an empty file called STOP in a job directory.

        This is used in the current Airflow setup to stop a dag.

        Args:
            user_id: The identifier of the user.
            job_id: The identifier of the job.
        """
        job_folder = self.get_job_id_folder(user_id, job_id)
        open(os.path.join(job_folder, 'STOP'), 'a').close()
        LOGGER.info(f"STOP file added to job folder {job_folder}.")

    @rpc
    def delete_complete_job(self, user_id: str, job_id: str) -> None:
        """Delete the complete job folder of the given job.

        Args:
            user_id: The identifier of the user.
            job_id: The  identifier of the job.
        """
        job_folder = self.get_job_id_folder(user_id, job_id)
        shutil.rmtree(job_folder)
        LOGGER.info(f"Complete job folder for job {job_id} deleted.")

    @rpc
    def delete_job_without_results(self, user_id: str, job_id: str) -> bool:
        """Delete everything in the job folder but the results folder of the given job.

        Args:
            user_id: The identifier of the user.
            job_id: The  identifier of the job.

        Returns:
            Whether there are results available or not.
        """
        job_result_folder = self.get_job_results_folder(user_id, job_id)
        if os.listdir(job_result_folder) == 0:
            LOGGER.info(f"No results exist for job {job_id}.")
            self.delete_complete_job(user_id, job_id)
            self.setup_jobs_result_folder(user_id, job_id)
        else:
            LOGGER.info(f"Job {job_id} has results.")
            bak_result_folder = os.path.join(self.get_user_folder(user_id=user_id), f"{job_id}_backup")
            os.makedirs(bak_result_folder)
            LOGGER.debug(f"Results backup folder created for job {job_id}.")

            os.rename(job_result_folder, bak_result_folder)
            self.delete_complete_job(user_id, job_id)
            self.setup_jobs_result_folder(user_id, job_id)
            os.rename(bak_result_folder, job_result_folder)

            if os.path.isdir(bak_result_folder):
                shutil.rmtree(bak_result_folder)
            LOGGER.debug(f"Results backup folder delete for job {job_id}.")

        return os.listdir(job_result_folder) != 0

    def get_job_id_folder(self, user_id: str, job_id: str) -> str:
        """Create and return the complete path to a specific job folder.

        Args:
            user_id: The identifier of the user.
            job_id: The  identifier of the job.

        Returns:
            File system path to a specific job folder of a user.
        """
        return os.path.join(self.get_user_folder(user_id), self.jobs_folder, job_id)

    def get_job_results_folder(self, user_id: str, job_id: str) -> str:
        """Create and return the path to a result folder in a specific job folder.

        Arg:
            user_id: The identifier of the user.
            job_id: The  identifier of the job.

        Returns:
            File system path to the results folder of a specific job of a user.
        """
        return os.path.join(self.get_job_id_folder(user_id, job_id), self.result_folder)
