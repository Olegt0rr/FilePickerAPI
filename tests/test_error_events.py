"""Тесты «одна ошибка — одно событие» на обработчиках и Uvicorn."""

import asyncio
import errno
import logging
import os
import socket
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import FileResponse
from uvicorn.protocols.utils import ClientDisconnected

from app import event_tracking, logging_setup
from app.handlers import files
from tests.conftest import win_error
from tests.test_main import fail_stat_for, reload_app


@pytest.fixture
def events(caplog):
    """Записи журнала с кодом события: (код, запись)."""
    caplog.set_level(logging.INFO, logger="app.event_tracking")

    def collect():
        return [(r.event_id, r) for r in caplog.records if getattr(r, "event_id", None)]

    return collect


@pytest.fixture
def files_dir(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "a.txt").write_text("content A")
    return folder


@pytest.fixture
def client(files_dir):
    return TestClient(reload_app(files_directory=str(files_dir)))


class TestOneErrorOneEvent:
    """Ошибка, превращённая в ответ, даёт ровно одно событие."""

    def test_list_files_unexpected_error(self, client, events):
        """500 с прежним detail и одно 4000 с трассировкой."""
        with mock.patch("os.scandir", side_effect=RuntimeError("Boom")):
            response = client.get("/files")

        assert response.status_code == 500
        assert response.json()["detail"] == (
            "Unexpected error when reading directory: Boom"
        )
        [(event_id, record)] = events()
        assert event_id == 4000
        assert record.exc_info[1].args == ("Boom",)
        assert record.getMessage().endswith(": RuntimeError: Boom")

    def test_os_error_as_500(self, client, events):
        """Ошибка ОС, превращённая в 500, — одно событие 2000."""
        with mock.patch("os.scandir", side_effect=OSError(errno.EIO, "Disk error")):
            response = client.get("/files")
        assert response.status_code == 500
        assert [e for e, _ in events()] == [2000]

    def test_file_read_error(self, client, events, caplog):
        """Ошибка чтения файла — одно 3000 и одна строка в журнале."""
        error = PermissionError(errno.EACCES, "Permission denied")
        with fail_stat_for("a.txt", error):
            response = client.get("/files/a.txt")
        assert response.status_code == 403
        assert [e for e, _ in events()] == [3000]
        lines = [r for r in caplog.records if r.name == "app.event_tracking"]
        assert len(lines) == 1

    def test_missing_file_no_event(self, client, events):
        """404 для отсутствующего файла при доступной папке — без
        события.
        """
        assert client.get("/files/missing.txt").status_code == 404
        assert events() == []

    def test_network_error_on_download(self, client, events):
        """«Сетевой путь не найден» при скачивании — 404 и 2000."""
        with fail_stat_for("a.txt", win_error(53, FileNotFoundError)):
            response = client.get("/files/a.txt")
        assert response.status_code == 404
        assert [e for e, _ in events()] == [2000]

    def test_slow_unavailable_folder(self, client, clock, events):
        """Первый запрос к недоступной папке дольше 10 секунд — и 2000,
        и 2003.
        """
        original = Path.stat

        def slow_stat(self, *args, **kwargs):
            if self.name == "files":
                clock.now += 11
                raise FileNotFoundError(errno.ENOENT, "No such file", str(self))
            return original(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", slow_stat):
            response = client.get("/files")
        assert response.status_code == 404
        assert [e for e, _ in events()] == [2000, 2003]

    def test_restored_after_unavailable(self, client, files_dir, events):
        """Список после недоступности — 2001 с числом ответов."""
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            for _ in range(3):
                assert client.get("/files").status_code == 403
        assert client.get("/files").status_code == 200
        assert client.get("/files/a.txt").status_code == 200

        sent = events()
        assert [e for e, _ in sent] == [2000, 2001]
        assert sent[1][1].getMessage().endswith("ошибочных ответов: 3")
        assert str(files_dir) in sent[1][1].getMessage()

    @pytest.mark.parametrize(("file_id", "status"), [("b.csv", 403), ("sub", 400)])
    def test_restored_by_client_error(self, client, files_dir, events, file_id, status):
        """Ответ 4xx, для которого папка ответила, — тоже 2001."""
        (files_dir / "b.csv").write_text("csv")
        (files_dir / "sub").mkdir()
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            assert client.get("/files").status_code == 403
        assert client.get(f"/files/{file_id}").status_code == status
        assert [e for e, _ in events()] == [2000, 2001]

    @pytest.mark.parametrize(
        ("error", "restored"),
        [
            (win_error(2, FileNotFoundError), True),
            # Пропавшая папка или отключённый диск на Windows
            (win_error(3, FileNotFoundError), False),
            # На других ОС ENOENT и у пропавшей папки
            (FileNotFoundError(errno.ENOENT, "No such file"), False),
        ],
    )
    def test_missing_file_restores_only_by_code(self, client, events, error, restored):
        """404 «File not found» — 2001 только по коду Windows 2."""
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            assert client.get("/files").status_code == 403
        with fail_stat_for("a.txt", error):
            assert client.get("/files/a.txt").status_code == 404
        expected = [2000, 2001] if restored else [2000]
        assert [e for e, _ in events()] == expected

    def test_fs_calls_not_increased(self, client, files_dir):
        """Учёт не добавляет обращений к ФС: один scandir на список."""
        scandir = mock.Mock(wraps=__import__("os").scandir)
        with mock.patch("os.scandir", scandir):
            assert client.get("/files").status_code == 200
        scandir.assert_called_once_with(files_dir)

    def test_download_stat_calls_not_increased(self, client, files_dir):
        """Учёт не добавляет обращений к файлу при скачивании: их
        столько же, сколько у самих проверок обработчика (на Windows —
        lstat, на других ОС — resolve() и stat). Сколько stat и lstat
        внутри делает pathlib, зависит от версии Python.
        """
        path = files_dir / "a.txt"

        def file_calls(action):
            stat = mock.Mock(wraps=os.stat)
            lstat = mock.Mock(wraps=os.lstat)
            with mock.patch("os.stat", stat), mock.patch("os.lstat", lstat):
                action()
            calls = stat.call_args_list + lstat.call_args_list
            return sum(Path(c.args[0]).name == "a.txt" for c in calls)

        if sys.platform == "win32":
            expected = file_calls(path.lstat)
        else:
            expected = file_calls(lambda: path.resolve().stat())
        assert expected
        assert file_calls(lambda: client.get("/files/a.txt")) == expected

    def test_slow_scandir(self, client, clock, events):
        """Медленный os.scandir — 2003 о списке."""
        original = os.scandir

        def slow_scandir(path):
            clock.now += 11
            return original(path)

        with mock.patch("os.scandir", slow_scandir):
            assert client.get("/files").status_code == 200
        [(event_id, record)] = events()
        assert event_id == 2003
        assert "GET /files за 11.0 с" in record.getMessage()

    def test_slow_file_stat(self, client, clock, events):
        """Медленные атрибуты файла (lstat на Windows) — 2003 о
        скачивании.
        """
        # Медленно только первое обращение к файлу: через os.stat или
        # os.lstat — зависит от ОС и версии Python
        slowed = []

        def slower(original):
            def call(path, *args, **kwargs):
                if Path(path).name == "a.txt" and not slowed:
                    slowed.append(path)
                    clock.now += 11
                return original(path, *args, **kwargs)

            return call

        with (
            mock.patch("os.stat", slower(os.stat)),
            mock.patch("os.lstat", slower(os.lstat)),
        ):
            assert client.get("/files/a.txt").status_code == 200
        assert slowed
        [(event_id, record)] = events()
        assert event_id == 2003
        assert record.getMessage().endswith("за 11.0 с, размер файла 9 байт")


class TestTrackedFileResponse:
    """Чтение тела скачиваемого файла."""

    def test_slow_body(self, client, clock, events, caplog):
        """Быстрые метаданные и медленное чтение — 2003 с размером."""
        original = FileResponse.__call__

        async def slow_call(self, scope, receive, send):
            clock.now += 15
            await original(self, scope, receive, send)

        with mock.patch.object(FileResponse, "__call__", slow_call):
            response = client.get("/files/a.txt")
            partial = client.get("/files/a.txt", headers={"Range": "bytes=0-3"})
        assert response.content == b"content A"
        assert partial.content == b"cont"
        [(event_id, record)] = events()
        assert event_id == 2003
        assert record.getMessage().endswith("за 15.0 с, размер файла 9 байт")
        # Часть файла (повтор — только в файле журнала): без размера
        tracked = [r for r in caplog.records if r.name == "app.event_tracking"]
        assert tracked[-1].getMessage().endswith("за 15.0 с")

    @pytest.mark.parametrize(
        ("error", "event_id"),
        [(win_error(64), 2000), (OSError(errno.EIO, "read failed"), 3000)],
    )
    def test_body_read_error(self, client, events, error, event_id):
        """Сетевой код при чтении тела — 2000, прочая ошибка — 3000;
        исключение пробрасывается.
        """
        with (
            mock.patch.object(FileResponse, "__call__", side_effect=error),
            pytest.raises(OSError, match=str(error.strerror)) as raised,
        ):
            client.get("/files/a.txt")
        assert raised.value is error
        [(sent_id, record)] = events()
        assert sent_id == event_id
        assert event_tracking.is_tracked(error)
        # Заголовки не отправлены — клиент получит 500
        assert record.getMessage().endswith("Ответ клиенту: 500")
        assert record.exc_info[1] is error

    @pytest.mark.parametrize(
        ("headers", "status"), [({}, 200), ({"Range": "bytes=0-3"}, 206)]
    )
    def test_body_error_after_headers(self, client, events, headers, status):
        """Ошибка после заголовков — ответ оборван с их кодом."""
        error = OSError(errno.EIO, "read failed")

        async def failing_call(_self, _scope, _receive, send):
            await send({"type": "http.response.start", "status": status, "headers": []})
            raise error

        with (
            mock.patch.object(FileResponse, "__call__", failing_call),
            pytest.raises(OSError, match="read failed"),
        ):
            client.get("/files/a.txt", headers=headers)
        [(event_id, record)] = events()
        assert event_id == 3000
        assert record.getMessage().endswith(f"Ответ клиенту: {status}, ответ оборван")

    @staticmethod
    def send_file(files_dir, send, *, spec_version="2.4", receive=None):
        """Отправить a.txt через TrackedFileResponse напрямую (ASGI)."""
        path = files_dir / "a.txt"
        response = files.TrackedFileResponse(
            path=path,
            stat_result=path.stat(),
            file_id="a.txt",
            directory=files_dir,
            started=event_tracking.clock(),
        )
        scope = {
            "type": "http",
            "method": "GET",
            "headers": [],
            "asgi": {"spec_version": spec_version},
        }

        async def no_receive():
            await asyncio.Event().wait()

        asyncio.run(response(scope, receive or no_receive, send))

    @staticmethod
    def make_unavailable(files_dir):
        event_tracking.directory_error(files_dir, PermissionError("denied"), 403)

    def test_disconnect_before_body_not_restored(self, files_dir, events):
        """Клиент отключился после заголовков (ASGI 2.3): Starlette
        возвращается без исключения, но тело не отправлено — 2001 нет.
        """
        self.make_unavailable(files_dir)
        messages = []

        async def send(message):
            messages.append(message["type"])

        async def receive():
            return {"type": "http.disconnect"}

        self.send_file(files_dir, send, spec_version="2.3", receive=receive)
        assert "http.response.start" in messages
        assert [e for e, _ in events()] == [2000]

    @pytest.mark.parametrize(
        "error",
        [
            BrokenPipeError(errno.EPIPE, "Broken pipe"),
            win_error(64, ConnectionResetError),
        ],
    )
    def test_client_send_error_no_event(self, files_dir, events, error):
        """Ошибка отправки клиенту — не ошибка файла или папки."""

        async def send(message):
            if message["type"] == "http.response.body":
                raise error

        with pytest.raises(type(error), match=str(error.strerror)) as raised:
            self.send_file(files_dir, send)
        assert raised.value is error
        assert events() == []
        assert event_tracking.is_tracked(error)

    def test_file_body_error_restores(self, files_dir, events):
        """Несетевая ошибка чтения тела (файл ещё пишут) — папка
        ответила: 2001, затем 3000.
        """
        self.make_unavailable(files_dir)
        error = win_error(32, PermissionError)

        async def failing_call(_self, _scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise error

        async def send(_message):
            pass

        with (
            mock.patch.object(FileResponse, "__call__", failing_call),
            pytest.raises(PermissionError, match="system message"),
        ):
            self.send_file(files_dir, send)
        assert [e for e, _ in events()] == [2000, 2001, 3000]

    def test_whole_body_restores(self, files_dir, events):
        """Тело отправлено целиком — 2001."""
        self.make_unavailable(files_dir)

        async def send(_message):
            pass

        self.send_file(files_dir, send)
        assert [e for e, _ in events()] == [2000, 2001]

    def test_body_read_error_not_restored(self, client, events):
        """Атрибуты читаются, тело — нет: 2001 нет, повторы не
        пишутся; 2001 — после скачивания целиком.
        """
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            assert client.get("/files").status_code == 403
        error = win_error(64)
        with mock.patch.object(FileResponse, "__call__", side_effect=error):
            for _ in range(3):
                with pytest.raises(OSError, match=str(error.strerror)):
                    client.get("/files/a.txt")
        assert [e for e, _ in events()] == [2000, 2000]
        assert client.get("/files/a.txt").status_code == 200
        assert [e for e, _ in events()] == [2000, 2000, 2001]

    def test_client_disconnect_no_event(self, client, events):
        """Клиент оборвал скачивание — это не ошибка папки или файла."""
        error = ClientDisconnected()
        with (
            mock.patch.object(FileResponse, "__call__", side_effect=error),
            pytest.raises(ClientDisconnected),
        ):
            client.get("/files/a.txt")
        assert events() == []


class TestExceptionHandlers:
    """Обработчики Exception и HTTPException в create_app()."""

    @pytest.fixture
    def app_client(self, files_dir):
        app = reload_app(files_directory=str(files_dir))
        tracked = ValueError("уже учтено")
        event_tracking.mark_tracked(tracked)

        @app.get("/boom/{item}")
        def boom(item: str):
            msg = f"сбой {item}"
            raise RuntimeError(msg)

        @app.get("/tracked")
        def already_tracked():
            raise tracked

        @app.get("/file-os/{fileId}")
        def file_os():
            raise HTTPException(500) from OSError(errno.EIO, "I/O")

        @app.get("/dir-os")
        def dir_os():
            raise HTTPException(500) from OSError(errno.EIO, "I/O")

        @app.get("/value")
        def value():
            raise HTTPException(503, "busy") from ValueError("bad")

        @app.get("/no-cause")
        def no_cause():
            raise HTTPException(500, "plain")

        @app.get("/not-found")
        def not_found():
            raise HTTPException(404, "nothing")

        return TestClient(app, raise_server_exceptions=False)

    def test_unhandled_exception(self, app_client, events):
        """Исключение — 500 как раньше и одно 4000 с трассировкой;
        ключ повторов — маршрут, а не конкретный путь.
        """
        for item in ("a", "b"):
            response = app_client.get(f"/boom/{item}")
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
        [(event_id, record)] = events()
        assert event_id == 4000
        assert record.getMessage().startswith(
            "Непредвиденная ошибка запроса GET /boom/a: RuntimeError: сбой a"
        )
        assert record.exc_info is not None

    def test_http_error_uses_request_access(self, files_dir, events):
        """Обработчик HTTPException берёт номер обращения с начала
        запроса, тот же, что видел обработчик маршрута.
        """
        app = reload_app(files_directory=str(files_dir))
        seen = {}

        @app.get("/dir-os")
        def dir_os():
            seen["handler"] = event_tracking._access.get()  # noqa: SLF001
            raise HTTPException(500) from OSError(errno.EIO, "I/O")

        original = event_tracking.directory_error

        def recording(*args):
            seen["error"] = event_tracking._access.get()  # noqa: SLF001
            original(*args)

        with mock.patch.object(event_tracking, "directory_error", recording):
            response = TestClient(app).get("/dir-os")
        assert response.status_code == 500
        assert seen["handler"] is not None
        assert seen["error"] == seen["handler"]
        assert [e for e, _ in events()] == [2000]

    def test_tracked_exception_skipped(self, app_client, events):
        assert app_client.get("/tracked").status_code == 500
        assert events() == []

    @pytest.mark.parametrize(
        ("path", "status", "event_id"),
        [
            ("/file-os/a.txt", 500, 3000),
            ("/dir-os", 500, 2000),
            ("/value", 503, 4000),
            ("/no-cause", 500, 4000),
            ("/not-found", 404, None),
        ],
    )
    def test_http_exception(self, app_client, events, path, status, event_id):
        """5xx — событие по исходной причине, detail прежний."""
        response = app_client.get(path)
        assert response.status_code == status
        assert [e for e, _ in events()] == ([] if event_id is None else [event_id])


@pytest.fixture
def event_log(monkeypatch):
    """EventLogHandler с поддельным win32evtlog на корневом логгере."""
    module = types.ModuleType("win32evtlog")
    module.EVENTLOG_ERROR_TYPE = 1
    module.EVENTLOG_WARNING_TYPE = 2
    module.EVENTLOG_INFORMATION_TYPE = 4
    module.RegisterEventSource = mock.Mock(return_value="source")
    module.DeregisterEventSource = mock.Mock()
    module.ReportEvent = mock.Mock()
    monkeypatch.setitem(sys.modules, "win32evtlog", module)
    handler = logging_setup.EventLogHandler()
    handler.send_startup_events()
    root = logging.getLogger()
    root.addHandler(handler)
    yield module.ReportEvent
    root.removeHandler(handler)
    handler.close()


def test_reminder_has_formatted_text(event_log, clock):
    """Запись без кода с аргументами: ключ — шаблон, а в напоминании —
    текст первой записи, а не «%s».
    """
    logger = logging.getLogger("uvicorn.error")
    for limit in (5, 6, 7):
        logger.warning("Exceeded concurrency limit %s.", limit)
    clock.advance(3600)
    event_tracking.tick()

    texts = [call.args[5][0] for call in event_log.call_args_list]
    assert texts == [
        "uvicorn.error: Exceeded concurrency limit 5.",
        (
            "app.event_tracking: Повторялось ещё 2 раз с прошлой записи: "
            "uvicorn.error: Exceeded concurrency limit 5."
        ),
    ]


def reported_ids(report_event):
    return [call.args[3] for call in report_event.call_args_list]


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("условие не выполнилось за отведённое время")
        time.sleep(0.05)


@pytest.fixture
def live_server(files_dir):
    """Настоящий uvicorn.Server в фоновом потоке, log_config=None."""
    app = reload_app(files_directory=str(files_dir))

    @app.get("/boom")
    def boom():
        msg = "сбой в обработчике"
        raise RuntimeError(msg)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port
    server.should_exit = True
    thread.join(10)


class TestUvicorn:
    """Журнал настоящего Uvicorn."""

    def test_unhandled_exception_one_event(self, live_server, event_log, caplog):
        """Непредвиденное исключение — одно 4000, хотя запись
        uvicorn.error о нём есть в журнале.
        """
        caplog.set_level(logging.ERROR, logger="uvicorn.error")
        response = httpx.get(f"http://127.0.0.1:{live_server}/boom")
        assert response.status_code == 500
        wait_for(lambda: any(r.name == "uvicorn.error" for r in caplog.records))
        assert reported_ids(event_log) == [4000]

    def test_malformed_requests_one_event(self, live_server, event_log, caplog, clock):
        """100 некорректных HTTP-запросов — событие и напоминание."""
        caplog.set_level(logging.WARNING, logger="uvicorn.error")
        for _ in range(100):
            with socket.create_connection(("127.0.0.1", live_server)) as sock:
                sock.sendall(b"GARBAGE \x00\x01\r\n\r\n")
                sock.recv(1024)

        def warnings():
            return [
                r
                for r in caplog.records
                if r.name == "uvicorn.error" and r.levelno == logging.WARNING
            ]

        wait_for(lambda: len(warnings()) == 100)
        assert reported_ids(event_log) == [3001]

        clock.now += 3600
        event_tracking.tick()
        assert reported_ids(event_log) == [3001, 3001]
        text = event_log.call_args.args[5][0]
        assert text.startswith("app.event_tracking: Повторялось ещё 99 раз")
