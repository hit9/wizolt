from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import (
    Config,
)
from wizolt.session import Session


def session(tmp_path):
    session = Session(cwd=str(tmp_path))
    bootstrap_features(session)
    return session


def data_session(tmp_path):
    session = Session(cwd=str(tmp_path), config=Config(data_dir=str(tmp_path / ".data")))
    bootstrap_features(session)
    return session
