from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from sakuramedia_local_provider import qbittorrent
from typing_extensions import Self

from src.plugins.provider_protocol import (
    DownloadClientHandle,
    DownloadSubmission,
    LibraryHandle,
    ProviderOperationError,
)

HASH = "0123456789abcdef0123456789abcdef01234567"


class FakeQBClient:
    instances: ClassVar[list[FakeQBClient]] = []

    def __init__(
        self, *, host: str, username: str, password: str, **kwargs: object
    ) -> None:
        self.kwargs = kwargs
        self.add_calls: list[dict] = []
        self.tag_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.items: list[object] = []
        self.add_result: object = "Ok."
        self.login_error: Exception | None = None
        self.info_error: Exception | None = None
        self.info_results: list[object] = []
        self.directory_entries: list[object] = ["sentinel.txt"]
        self.directory_requests: list[str] = []
        self.logged_in = 0
        type(self).instances.append(self)

    def auth_log_in(self) -> None:
        self.logged_in += 1
        if self.login_error is not None:
            raise self.login_error

    def app_version(self) -> str:
        return "5.0.0"

    def app_web_api_version(self) -> str:
        return "2.8.3"

    def app_get_directory_content(self, path: str) -> list[object]:
        self.directory_requests.append(path)
        return self.directory_entries

    def torrents_add(self, **kwargs: object) -> object:
        self.add_calls.append(kwargs)
        if isinstance(self.add_result, Exception):
            raise self.add_result
        source_uri = kwargs.get("urls")
        info_hash = qbittorrent.parse_hash_from_magnet(source_uri) if isinstance(source_uri, str) else HASH
        self.items = [
            SimpleNamespace(
                hash=info_hash,
                name=kwargs["rename"],
                state="queuedDL",
                progress=0.0,
                tags=kwargs["tags"],
                content_path=kwargs["save_path"],
            )
        ]
        return self.add_result

    def torrents_add_tags(self, **kwargs: object) -> None:
        self.tag_calls.append(kwargs)

    def torrents_info(self, **kwargs: object) -> list[object]:
        if self.info_error is not None:
            raise self.info_error
        if self.info_results:
            result = self.info_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return list(result)  # type: ignore[arg-type]
        if "torrent_hashes" in kwargs:
            return self.items[:1]
        return self.items

    def torrents_delete(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path):
    FakeQBClient.instances.clear()
    module = types.ModuleType("qbittorrentapi")
    module.Client = FakeQBClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qbittorrentapi", module)
    library = LibraryHandle(
        library_id=1,
        provider_key="local",
        provider_config={
            "media_root_path": str(tmp_path / "media"),
        },
        account_key=None,
    )
    component = qbittorrent.QbittorrentDownloadComponent(data_dir=tmp_path / "plugin-data")
    config = component.prepare_client(
        submitted_config={
            "base_url": "http://qbittorrent.local/",
            "username": " admin ",
            "password": "secret",
            "remote_save_root": "/downloads",
            "backend_import_root_path": str(tmp_path / "downloads"),
        },
        library=library,
        previous=None,
    )
    handle = DownloadClientHandle(client_id=7, library=library, provider_config=config)
    return component.build(client=handle), FakeQBClient.instances[-1]


def test_config_fields_and_prepare_are_normalised_without_network(tmp_path: Path) -> None:
    component = qbittorrent.QbittorrentDownloadComponent(data_dir=tmp_path / "plugin-data")
    assert [field.key for field in component.config_fields] == [
        "base_url",
        "username",
        "password",
        "remote_save_root",
        "backend_import_root_path",
    ]
    assert component.config_fields[2].input == "secret"
    library = LibraryHandle(1, "local", {"media_root_path": "/media"}, None)
    prepared = component.prepare_client(
        submitted_config={
            "base_url": " https://qb.example/ ",
            "username": " user ",
            "password": " pass ",
            "remote_save_root": "//downloads//",
            "backend_import_root_path": "/mnt/qb-downloads",
        },
        library=library,
        previous=None,
    )
    assert prepared == {
        "base_url": "https://qb.example",
        "username": "user",
        "password": " pass ",
        "remote_save_root": "/downloads",
        "backend_import_root_path": "/mnt/qb-downloads",
    }
    with pytest.raises(ProviderOperationError) as error:
        component.prepare_client(
            submitted_config={**prepared, "remote_save_root": "/downloads/../secret"},
            library=library,
            previous=None,
        )
    assert error.value.code == "invalid_config"
    assert "/downloads" not in error.value.safe_message


def test_diagnostics_checks_connection_mapping_and_hardlink(provider) -> None:
    client, fake = provider
    backend_root = Path(client.backend_import_root_path)
    backend_root.mkdir(parents=True)

    report = client.run_diagnostics()

    assert report.status == "ok"
    assert [check.key for check in report.checks] == [
        "qbittorrent_connection",
        "directory_mapping",
        "hardlink",
    ]
    assert all(check.status == "ok" for check in report.checks)
    assert fake.directory_requests[0].startswith("/downloads/.sakuramedia-diagnostics/")
    assert not (backend_root / ".sakuramedia-diagnostics").exists()


def test_diagnostics_supports_media_and_download_roots_being_the_same(provider) -> None:
    client, _fake = provider
    root = Path(client.backend_import_root_path)
    root.mkdir(parents=True)
    client.client_handle.library.provider_config["media_root_path"] = str(root)

    report = client.run_diagnostics()

    assert report.status == "ok"
    assert [check.status for check in report.checks] == ["ok", "ok", "ok"]
    assert not (root / ".sakuramedia-diagnostics").exists()


def test_diagnostics_allows_save_warning_when_hardlink_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    client, _fake = provider
    Path(client.backend_import_root_path).mkdir(parents=True)
    monkeypatch.setattr(qbittorrent.os, "link", lambda *_args: (_ for _ in ()).throw(OSError()))

    report = client.run_diagnostics()

    assert report.status == "warning"
    assert [check.status for check in report.checks] == ["ok", "ok", "warning"]
    assert report.checks[-1].code == "hardlink_unavailable"


def test_diagnostics_fails_mapping_when_qb_cannot_see_sentinel(provider) -> None:
    client, fake = provider
    Path(client.backend_import_root_path).mkdir(parents=True)
    fake.directory_entries = []

    report = client.run_diagnostics()

    assert report.status == "failed"
    assert report.checks[1].code == "directory_mapping_mismatch"
    assert report.checks[2].status == "skipped"


@pytest.mark.parametrize(
    "directory_entry",
    [
        {"name": "sentinel.txt"},
        {"path": "/downloads/.sakuramedia-diagnostics/probe/sentinel.txt"},
        SimpleNamespace(path=r"C:\downloads\sentinel.txt"),
    ],
)
def test_diagnostics_accepts_qb_directory_entry_shapes(provider, directory_entry) -> None:
    client, fake = provider
    Path(client.backend_import_root_path).mkdir(parents=True)
    fake.directory_entries = [directory_entry]

    report = client.run_diagnostics()

    assert report.status == "ok"


def test_qb_state_mapping_keeps_unrelated_states_queued() -> None:
    assert qbittorrent.map_qb_state("checkingUP") == "completed"
    assert qbittorrent.map_qb_state("checkingResumeData") == "queued"
    assert qbittorrent.map_qb_state("moving") == "queued"


def test_magnet_hash_accepts_hex_and_base32() -> None:
    raw_hash = bytes(range(20))
    base32_hash = base64.b32encode(raw_hash).decode().rstrip("=").lower()
    assert qbittorrent.parse_hash_from_magnet(f"magnet:?xt=urn:btih:{HASH}") == HASH
    assert qbittorrent.parse_hash_from_magnet(f"magnet:?xt=urn:btih:{base32_hash}") == raw_hash.hex()


def test_submit_magnet_uses_managed_tags_and_safe_remote_path(provider) -> None:
    client, fake = provider
    task = client.submit(
        submission=DownloadSubmission(
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            display_name="ABC 001",
        )
    )
    assert task.remote_id == HASH
    assert task.state == "queued"
    assert fake.add_calls == [
        {
            "urls": f"magnet:?xt=urn:btih:{HASH}",
            "tags": "sakuramedia,client:7",
            "save_path": "/downloads/ABC 001",
            "rename": "ABC 001",
        }
    ]
    assert fake.logged_in == 1
    assert fake.kwargs == {
        "REQUESTS_ARGS": {"timeout": 30},
        "VERIFY_WEBUI_CERTIFICATE": True,
        "FORCE_SCHEME_FROM_HOST": True,
    }


def test_submit_torrent_downloads_url_and_submits_bytes(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    client, fake = provider

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-length": "13"}

        def iter_bytes(self):
            yield b"torrent-bytes"

    class Stream:
        def __init__(self, response: Response) -> None:
            self.response = response

        def __enter__(self) -> Response:
            return self.response

        def __exit__(self, *_args: object) -> None:
            return None

    class HTTPClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs == {"timeout": 120.0, "follow_redirects": True, "trust_env": False}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, method: str, url: str) -> Stream:
            assert method == "GET"
            assert url == "https://index.example/ABC.torrent"
            return Stream(Response())

    monkeypatch.setattr(qbittorrent.httpx, "Client", HTTPClient)
    lt_module = types.ModuleType("libtorrent")
    lt_module.torrent_info = lambda payload: SimpleNamespace(info_hash=lambda: HASH)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "libtorrent", lt_module)

    task = client.submit(
        submission=DownloadSubmission(
            source_uri="https://index.example/ABC.torrent",
            display_name="ABC 001",
        )
    )
    assert task.remote_id == HASH
    assert fake.add_calls[0]["torrent_files"] == b"torrent-bytes"
    assert "urls" not in fake.add_calls[0]


def test_submit_existing_torrent_returns_actual_managed_task_without_tagging(provider) -> None:
    client, fake = provider

    class Conflict409Error(Exception):
        pass

    fake.add_result = Conflict409Error("already exists")
    fake.items = [
        SimpleNamespace(
            hash=HASH,
            name="existing",
            state="checkingUP",
            progress=0.8,
            tags="sakuramedia,client:7",
            content_path="/downloads/existing",
        )
    ]
    task = client.submit(
        submission=DownloadSubmission(
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            display_name="ABC",
        )
    )
    assert task.remote_id == HASH
    assert task.state == "completed"
    assert task.name == "existing"
    assert task.completed_source_ref == {
        "version": 1,
        "kind": "download_local_path",
        "root_path": client.backend_import_root_path,
        "relative_path": "existing",
    }
    assert fake.tag_calls == []


def test_submit_retries_short_visibility_window_after_success(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    client, fake = provider
    final_item = SimpleNamespace(
        hash=HASH,
        name="visible",
        state="queuedDL",
        progress=0.0,
        tags="sakuramedia,client:7",
        content_path="/downloads/visible",
    )
    fake.info_results = [[], [], [final_item]]
    sleep_calls: list[float] = []
    monkeypatch.setattr(qbittorrent.time, "sleep", sleep_calls.append)
    task = client.submit(
        submission=DownloadSubmission(
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            display_name="visible",
        )
    )
    assert task.remote_id == HASH
    assert sleep_calls == [0.1, 0.1]


def test_submit_existing_unmanaged_torrent_is_rejected_without_tagging(provider) -> None:
    client, fake = provider

    class Conflict409Error(Exception):
        pass

    fake.add_result = Conflict409Error("already exists")
    fake.items = [SimpleNamespace(hash=HASH, tags="sakuramedia,client:99")]
    with pytest.raises(ProviderOperationError) as error:
        client.submit(
            submission=DownloadSubmission(
                source_uri=f"magnet:?xt=urn:btih:{HASH}",
                display_name="ABC",
            )
        )
    assert error.value.code == "task_not_managed"
    assert fake.tag_calls == []


def test_submit_rejects_blacklisted_hash(provider) -> None:
    client, fake = provider
    client.dead_hashes_path.parent.mkdir(parents=True)
    client.dead_hashes_path.write_text(json.dumps([HASH]), encoding="utf-8")

    with pytest.raises(ProviderOperationError) as error:
        client.submit(
            submission=DownloadSubmission(
                source_uri=f"magnet:?xt=urn:btih:{HASH}",
                display_name="ABC",
            )
        )

    assert error.value.code == "source_blacklisted"
    assert error.value.retryable is False
    assert fake.add_calls == []


def test_list_maps_states_filters_tags_and_projects_relative_ref(provider) -> None:
    client, fake = provider
    fake.items = [
        SimpleNamespace(
            hash=HASH.upper(),
            name="ABC",
            state="uploading",
            progress=0.4,
            tags="sakuramedia,client:7",
            content_path="/downloads/ABC",
        ),
        SimpleNamespace(
            hash="89abcdef0123456789abcdef0123456789abcdef",
            name="Downloading",
            state="metaDL",
            progress=0.2,
            tags=["sakuramedia", "client:7"],
            content_path="/downloads/Downloading",
        ),
        SimpleNamespace(
            hash="fedcba9876543210fedcba9876543210fedcba98",
            name="Ignored",
            state="uploading",
            progress=1,
            tags="sakuramedia,client:99",
            content_path="/downloads/Ignored",
        ),
        SimpleNamespace(
            hash="abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            name="Broken",
            state="uploading",
            progress=1,
            tags="sakuramedia,client:7",
            content_path="/other/Broken",
        ),
    ]
    tasks = client.list_tasks()
    assert [task.remote_id for task in tasks] == [
        HASH,
        "89abcdef0123456789abcdef0123456789abcdef",
        "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
    ]
    assert tasks[0].state == "completed"
    assert tasks[0].progress == 1
    assert tasks[0].completed_source_ref == {
        "version": 1,
        "kind": "download_local_path",
        "root_path": client.backend_import_root_path,
        "relative_path": "ABC",
    }
    assert tasks[1].state == "downloading"
    assert tasks[1].completed_source_ref is None
    assert tasks[2].state == "failed"
    assert tasks[2].remote_id == "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert tasks[2].completed_source_ref is None


def test_list_removes_torrent_after_day_without_progress(monkeypatch, provider) -> None:
    client, fake = provider
    now = 1_000_000
    monkeypatch.setattr(qbittorrent.time, "time", lambda: now)
    fake.items = [
        SimpleNamespace(
            hash=HASH,
            name="Dead",
            state="downloading",
            progress=0.2,
            dlspeed=0,
            last_activity=now - qbittorrent.DEAD_TORRENT_IDLE_SECONDS,
            tags="sakuramedia,client:7",
            content_path="/downloads/Dead",
        )
    ]

    assert client.list_tasks() == ()
    assert fake.delete_calls == [{"torrent_hashes": HASH, "delete_files": True}]
    assert json.loads(client.dead_hashes_path.read_text(encoding="utf-8")) == [HASH]


@pytest.mark.parametrize("state", ["error", "missingFiles"])
def test_list_blacklists_and_removes_qb_failed_torrent(provider, state) -> None:
    client, fake = provider
    fake.items = [
        SimpleNamespace(
            hash=HASH,
            name="Failed",
            state=state,
            progress=0.2,
            tags="sakuramedia,client:7",
            content_path="/downloads/Failed",
        )
    ]

    assert client.list_tasks() == ()
    assert fake.delete_calls == [{"torrent_hashes": HASH, "delete_files": True}]
    assert json.loads(client.dead_hashes_path.read_text(encoding="utf-8")) == [HASH]


@pytest.mark.parametrize(
    ("dlspeed", "last_activity"),
    [(0, 913_601), (1, 913_600)],
)
def test_list_keeps_torrent_with_recent_or_current_progress(
    monkeypatch, provider, dlspeed, last_activity
) -> None:
    client, fake = provider
    monkeypatch.setattr(qbittorrent.time, "time", lambda: 1_000_000)
    fake.items = [
        SimpleNamespace(
            hash=HASH,
            name="Live",
            state="stalledDL",
            progress=0.2,
            dlspeed=dlspeed,
            last_activity=last_activity,
            tags="sakuramedia,client:7",
            content_path="/downloads/Live",
        )
    ]

    assert client.list_tasks()[0].remote_id == HASH
    assert fake.delete_calls == []
    assert not client.dead_hashes_path.exists()


def test_delete_only_managed_tasks_and_is_idempotent(provider) -> None:
    client, fake = provider
    fake.items = [SimpleNamespace(hash=HASH, tags="sakuramedia,client:7")]
    client.delete_task(remote_id=HASH, delete_files=True)
    assert fake.delete_calls == [{"torrent_hashes": HASH, "delete_files": True}]

    fake.items = [SimpleNamespace(hash=HASH, tags="sakuramedia,client:99")]
    with pytest.raises(ProviderOperationError) as error:
        client.delete_task(remote_id=HASH, delete_files=False)
    assert error.value.code == "task_not_managed"
    assert HASH not in error.value.safe_message

    class NotFound404Error(Exception):
        pass

    fake.info_error = NotFound404Error("missing")
    client.delete_task(remote_id=HASH, delete_files=False)


def test_upstream_errors_are_structured_and_safe(provider) -> None:
    client, fake = provider
    class LoginFailed(Exception):
        pass

    fake.login_error = LoginFailed("password=secret http://qb.example")
    with pytest.raises(ProviderOperationError) as error:
        client.list_tasks()
    assert error.value.code == "authentication_failed"
    assert "secret" not in error.value.safe_message
    assert "qb.example" not in error.value.safe_message


def test_connection_errors_are_retryable_unavailable(provider) -> None:
    client, fake = provider
    fake.login_error = ConnectionError("qb unavailable")
    with pytest.raises(ProviderOperationError) as error:
        client.list_tasks()
    assert error.value.code == "unavailable"
    assert error.value.retryable is True
