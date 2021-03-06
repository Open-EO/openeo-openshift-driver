"""Provide functionality to handle package settings."""

import logging
from enum import Enum
from os import makedirs
from os.path import isdir
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dynaconf import Validator, settings

LOGGER = logging.getLogger('standardlog')


class SettingKeys(Enum):
    """Holds all required setting keys with description."""

    CACHE_PATH = "CACHE_PATH"
    """The path to a directory where collection metadata should be cached.

    If you are running in docker the path needs to be inside the container.
    """

    IS_CSW_SERVER = "IS_CSW_SERVER"
    """The flag for a CSW server.

    It flags if to expect the environment variables related to CSW_SERVER.
    """
    CSW_SERVER = "CSW_SERVER"
    """The url to a running CSW server.

    E.g. eodc's CSW server is reachable under https://csw.eodc.eu
    If you are running in docker and also have a CSW server in the same docker network it could be http://pycsw:8000
    """
    DATA_ACCESS = "DATA_ACCESS"
    """The required user permissions to access data from this CSW server.

    Each user has a profile and with this a 'data_access' associated to him-/herself e.g. 'public' or 'public,orga-a'.
    A CSW server with DATA_ACCESS 'orga-a' is only accessible for user with this 'data_access'.
    """
    GROUP_PROPERTY = "GROUP_PROPERTY"
    """The CSW property to query for when searching for a collection identifier.

    As different data sources may be structured differently in the metadata DB this parameter can be used to defined
    which field to query when searching for a given 'identifier'. E.g. the collection name can either be stored as
    identifier ('apiso:Parentidentifier') or as variable name ('eodc:variable_name').
    """
    WHITELIST = "WHITELIST"
    """A list collections on the CSW server which should be accessible via OpenEO.

    There are scenarios where not all collections available on a CSW server are meaningful in the context of OpenEO.
    """

    IS_CSW_SERVER_DC = "IS_CSW_SERVER_DC"
    """The flag for a second CSW server.

    It flags if to expect the environment variables related to CSW_SERVER_DC.
    """
    CSW_SERVER_DC = "CSW_SERVER_DC"
    """The url to a second running CSW server.

    This is basically the same as :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER`.
    To enable the use of two (exactly TWO) CSW servers this and all other parameters with the suffix `_DC` are used.
    Their meaning is completely equivalent to the parameters without the suffix just describing a different CSW server.
    """
    DATA_ACCESS_DC = "DATA_ACCESS_DC"
    """See :attr:`~data.dependencies.settings.SettingKeys.DATA_ACCESS` and description in
    :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER_DC`.
    """
    GROUP_PROPERTY_DC = "GROUP_PROPERTY_DC"
    """See :attr:`~data.dependencies.settings.SettingKeys.GROUP_PROPERTY` and description in
    :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER_DC`.
    """
    WHITELIST_DC = "WHITELIST_DC"
    """See :attr:`~data.dependencies.settings.SettingKeys.WHITELIST` and description in
    :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER_DC`.
    """

    IS_HDA_WEKEO = "IS_HDA_WEKEO"
    """The flag for the connection to WEkEO's HDA API.

    It flags if to expect the environment variables related to WEKEO_API_URL.
    """
    WEKEO_API_URL = "WEKEO_API_URL"
    """The url to WEkEO's HDA API.
    """
    WEKEO_USER = "WEKEO_USER"  # noqa S105
    """The username for a WEkEO instance.
    """
    WEKEO_PASSWORD = "WEKEO_PASSWORD"  # noqa S105
    """The password for a WEkEO instance.
    """
    WEKEO_STORAGE = "WEKEO_STORAGE"
    """The path where files downloaded via the WEkEO HDA API will be available
    on the VM, where the processing engine (e.g. Airflow) executes jobs."""
    DATA_ACCESS_WEKEO = "DATA_ACCESS_WEKEO"
    """See :attr:`~data.dependencies.settings.SettingKeys.DATA_ACCESS` and description in
    :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER_DC`.
    """
    WHITELIST_WEKEO = "WHITELIST_WEKEO"
    """See :attr:`~data.dependencies.settings.SettingKeys.WHITELIST` and description in
    :attr:`~data.dependencies.settings.SettingKeys.CSW_SERVER_DC`.
    """

    # Connection to RabbitMQ
    RABBIT_HOST = "RABBIT_HOST"
    """The host name of the RabbitMQ - e.g. `rabbitmq`.

    If you are running in docker this is the hostname of the container!
    """
    RABBIT_PORT = "RABBIT_PORT"
    """The port on which the RabbitMQ is running - e.g. `5672`.

    If you are running in docker and the capabilities container is in the same network as the RabbitMQ this is the port
    inside the docker network NOT the exposed one!
    """
    RABBIT_USER = "RABBIT_USER"
    """The username to authenticate on the RabbitMQ - e.g. `rabbitmq`."""
    RABBIT_PASSWORD = "RABBIT_PASSWORD"  # noqa S105
    """The password to authenticate with the given user on the RabbitMQ."""

    # Additional
    LOG_DIR = "LOG_DIR"
    """The path to the directory where log files should be saved.

    If you are running in docker this is the path inside the docker container! E.g. `/usr/src/logs`
    In case you want to persist the logs a volume or a local folder needs to be mounted into the specified location.
    """


class SettingValidationUtils:
    """Provides a set of utility functions to validated settings."""

    def check_create_folder(self, folder_path: str) -> bool:
        """Create the given folder path if it does not exist, always returns True."""
        if not isdir(folder_path):
            makedirs(folder_path)
        return True

    def check_url_is_reachable(self, url: str) -> bool:
        """Return a boolean whether a connection to a given url could be created."""
        try:
            if url.lower().startswith('http'):
                req = Request(url)
                with urlopen(req) as resp:  # noqa
                    return resp.status == 200
            else:
                return False
        except URLError:
            return False

    def check_parse_url(self, url: str) -> bool:
        """Return a boolean whether the url could be parsed.

        This is useful if a setting holding a url may not be reachable at the time of setting validation. Then this
        method at least validates that a valid url is provided. E.g. the gateway will most probably be not reachable
        when bringing up microservices.
        """
        result = urlparse(url)
        return all([result.scheme, result.netloc])


def initialise_settings() -> None:
    """Configure and validates settings.

    This method is called when starting the microservice to ensure all configuration settings are properly provided.

    Raises:
        :class:`~dynaconf.validator.ValidationError`: A setting is not valid.
    """
    not_doc = Validator("ENV_FOR_DYNACONF", is_not_in=["documentation"])
    not_doc_unittest = Validator("ENV_FOR_DYNACONF", is_not_in=["documentation", "unittest"])
    settings.configure(ENVVAR_PREFIX_FOR_DYNACONF="OEO")
    utils = SettingValidationUtils()

    settings.validators.register(
        Validator(SettingKeys.CACHE_PATH.value, must_exist=True, condition=utils.check_create_folder, when=not_doc),

        Validator(SettingKeys.IS_CSW_SERVER.value, default=False),
        Validator(SettingKeys.CSW_SERVER.value, SettingKeys.DATA_ACCESS.value,
                  SettingKeys.GROUP_PROPERTY.value, SettingKeys.WHITELIST.value,
                  must_exist=True, when=Validator(SettingKeys.IS_CSW_SERVER.value, eq=True) & not_doc),

        Validator(SettingKeys.IS_CSW_SERVER_DC.value, default=False),
        Validator(SettingKeys.CSW_SERVER_DC.value, SettingKeys.DATA_ACCESS_DC.value,
                  SettingKeys.GROUP_PROPERTY_DC.value, SettingKeys.WHITELIST_DC.value,
                  must_exist=True, when=Validator(SettingKeys.IS_CSW_SERVER_DC.value, eq=True) & not_doc),

        Validator(SettingKeys.IS_HDA_WEKEO.value, default=False),
        Validator(SettingKeys.WEKEO_API_URL.value, SettingKeys.WEKEO_STORAGE.value,
                  SettingKeys.DATA_ACCESS_WEKEO.value, SettingKeys.WHITELIST_WEKEO.value,
                  must_exist=True, when=Validator(SettingKeys.IS_HDA_WEKEO.value, eq=True) & not_doc),
        Validator(SettingKeys.WEKEO_USER.value, SettingKeys.WEKEO_PASSWORD.value,
                  must_exist=True, when=Validator(SettingKeys.IS_HDA_WEKEO.value, eq=True) & not_doc_unittest),

        Validator(SettingKeys.RABBIT_HOST.value, must_exist=True, when=not_doc_unittest),
        Validator(SettingKeys.RABBIT_PORT.value, must_exist=True, is_type_of=int, when=not_doc_unittest),
        Validator(SettingKeys.RABBIT_USER.value, must_exist=True, when=not_doc_unittest),
        Validator(SettingKeys.RABBIT_PASSWORD.value, must_exist=True, when=not_doc_unittest),

        Validator(SettingKeys.LOG_DIR.value, must_exist=True, condition=utils.check_create_folder,
                  when=not_doc_unittest),
    )
    settings.validators.validate()
    if not (settings.IS_CSW_SERVER or settings.IS_CSW_SERVER_DC or settings.IS_HDA_WEKEO):
        raise Exception("No (meta)data connector is specified. At least one of the"
                        "following env variables must be true: OEO_IS_CSW_SERVER,"
                        " OEO_IS_CSW_SERVER_DC, OEO_IS_HDA_WEKEO.")

    if settings.ENV_FOR_DYNACONF != "documentation":
        if settings.IS_CSW_SERVER:
            settings.WHITELIST = settings.WHITELIST.split(",")
        if settings.IS_CSW_SERVER_DC:
            settings.WHITELIST_DC = settings.WHITELIST_DC.split(",")
        if settings.IS_HDA_WEKEO:
            settings.WHITELIST_WEKEO = settings.WHITELIST_WEKEO.split(",")

    LOGGER.info("Settings validated")
