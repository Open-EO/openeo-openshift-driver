"""This domain module provides the database model for the user database.

This includes all required table definitions and auxiliary data structures.
"""
import enum
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship


class AuthType(enum.Enum):
    """Enum providing all types of authentication."""

    basic = 0
    oidc = 1


Base: Any = declarative_base()


class Users(Base):
    """DB table definition for a user."""

    __tablename__ = 'users'

    id = Column(String, primary_key=True)  # noqa A003
    """Unique string identifier of a user."""
    auth_type = Column(Enum(AuthType), nullable=False)
    """The authentication type (basic / oidc) connected to this user as enum."""
    role = Column(Text, nullable=False, default='user')
    """The user's role (user / admin) as string."""
    username = Column(Text, unique=True)
    """A unique username for the user - required for basic auth."""
    password_hash = Column(Text)
    """The hashed user password as a string - required for basic auth."""
    email = Column(Text, unique=True)
    """The email address of the user - unique key - required for oidc auth."""
    identity_provider_id = Column(String, ForeignKey('identity_providers.id'))
    """The string id of the connected identity provider (ForeignKey) - required fir oidc auth."""
    profile_id = Column(String, ForeignKey('profiles.id'), nullable=False)
    """The string id of the connected profile (ForeignKey)."""
    budget = Column(Integer)
    """The user's budget (optional)."""
    name = Column(Text)
    """A human-friendly name for the user (optional)."""
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    """UTC datetime of the creation of the records."""
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    """UTC datetime of the last modification of this record."""

    storage = relationship('Storage', uselist=False, cascade='all, delete, delete-orphan')
    """Specification about the user's connected storage :class:`~users.models.Storage`."""
    links = relationship('Links', cascade='all, delete, delete-orphan')
    """A list of related :class:`~users.models.Links`."""


class IdentityProviders(Base):
    """DB table definition for an identity provider."""

    __tablename__ = 'identity_providers'

    id = Column(String, primary_key=True)  # noqa A003
    """A unique string id of the identity provider."""
    id_openeo = Column(String, nullable=False, unique=True)
    """A unique name of the identity provider."""
    issuer_url = Column(Text, nullable=False)
    """The issuer url."""
    scopes = Column(Text, nullable=False)
    """A comma separate list of scopes."""
    title = Column(String, nullable=False, unique=True)
    """A unique title."""
    description = Column(Text)
    """A longer description (optional)."""

    links = relationship('Links', cascade='all, delete, delete-orphan')
    """A list of related :class:`~users.models.Links`."""


class Links(Base):
    """DB table definition for a link."""

    __tablename__ = 'links'

    id = Column(Integer, primary_key=True)  # noqa A003
    """A unique integer id of the link."""
    identity_provider_id = Column(String, ForeignKey('identity_providers.id'))
    """The id of the related identity provider (ForgeinKey - optional)."""
    user_id = Column(String, ForeignKey('users.id'))
    """The id of the related user (ForeignKey - optional)."""
    rel = Column(String, nullable=False)
    """Relationship between the current document and the linked document."""
    href = Column(String, nullable=False)  # should be uri!
    """A valid URL."""
    type = Column(String, nullable=True)  # noqa A003
    """A string that hints at the format used to represent data at the provided URI (optional)."""
    title = Column(String, nullable=True)
    """Used as a human-readable label for a link (optional)."""


class Profiles(Base):
    """DB table definition for a user profile."""

    __tablename__ = 'profiles'

    # To be extended as needed
    id = Column(String, primary_key=True)  # noqa A003
    """A unique string identifier of the profile."""
    name = Column(Text, nullable=False, unique=True)
    """A human readable name."""
    data_access = Column(Text, nullable=False)
    """A comma separate list of defined data_access levels."""


class Storage(Base):
    """DB table definition for storage."""

    __tablename__ = 'storage'

    id = Column(Integer, primary_key=True)  # noqa A003
    """A unique integer id of te storage."""
    user_id = Column(String, ForeignKey('users.id'), nullable=False)
    """The id of the related user (ForeignKey)."""
    free = Column(Integer, nullable=False)
    """Free storage space in bytes, which is still available to the user."""
    quota = Column(Integer, nullable=False)
    """Maximum storage space (disk quota) in bytes available to the user."""
