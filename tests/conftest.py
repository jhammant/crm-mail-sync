from dataclasses import replace
from pathlib import Path
import socket

import pytest

from crm_mail_sync.adapters.fake import FakeCrm
from crm_mail_sync.adapters.fixture import FixtureSource
from crm_mail_sync.adapters.review_jsonl import JsonlReviewSink
from crm_mail_sync.config import load_config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def config(tmp_path):
    return replace(load_config('examples/config.example.yaml'), state_path=tmp_path/'state.json',
                   review_path=tmp_path/'review.jsonl', gmail_token_path=tmp_path/'token.json')


@pytest.fixture
def source():
    return FixtureSource('fixtures/sample-mailbox.json')


@pytest.fixture
def crm(source):
    return FakeCrm(source.data['crm'])


@pytest.fixture
def review(config):
    return JsonlReviewSink(config.review_path)
