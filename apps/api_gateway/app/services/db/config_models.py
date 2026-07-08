from __future__ import annotations
from sqlalchemy import Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ConfigBase(DeclarativeBase):
    pass


class ProfileRow(ConfigBase):
    __tablename__ = "config_profiles"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class TtsProfileRow(ConfigBase):
    __tablename__ = "config_tts_profiles"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class McpServerRow(ConfigBase):
    __tablename__ = "config_mcp_servers"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class SystemRow(ConfigBase):
    __tablename__ = "config_system"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text)
