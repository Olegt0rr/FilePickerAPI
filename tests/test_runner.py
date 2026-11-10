"""Тесты экземпляра сервиса: запуск, второй экземпляр, остановка.

run() выполняется в процессе теста: выход процесса (_exit) и
обработчики сигналов подменены (фикстура service_env), журнал событий —
поддельный win32evtlog. Uvicorn в части тестов настоящий, на свободном
порте вместо 8000.
"""

import contextlib
import ctypes
import functools
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI

import app
from app import event_tracking, instance_check, logging_setup, runner, stop_signals
from app.instance_check import Probe, ProbeResult
from tests.conftest import child_env, flush_event_logs, start_then_stop, wait_for
from tests.platforms import linux_only, windows_only
from tests.test_main import reload_app, run_python

# Настоящая установка обработчиков: фикстура service_env её подменяет
INSTALL = stop_signals.install

# Сокет, занятый «другой программой»: на Windows привязка к тому же
# адресу без SO_EXCLUSIVEADDRUSE могла бы пройти рядом с ним
EXCLUSIVE = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def blocker(port):
    """Слушающий сокет, который не отвечает по HTTP."""
    sock = socket.socket()
    if EXCLUSIVE is not None:
        sock.setsockopt(socket.SOL_SOCKET, EXCLUSIVE, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    return sock


class OpenApiServer(ThreadingHTTPServer):
    """Поддельный File Picker API: отвечает только на /openapi.json."""

    # На Windows SO_REUSEADDR позволил бы Uvicorn занять тот же порт
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def __init__(self, port, *, title="File Picker API", version=None, fail=0):
        self.title = title
        self.version = app.__version__ if version is None else version
        # Сколько первых запросов получат 503 (сервис ещё запускается)
        self.fail = fail
        self.requests = 0
        super().__init__(("127.0.0.1", port), OpenApiHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown()
        self.server_close()


class OpenApiHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.requests += 1
        if self.path != "/openapi.json" or server.requests <= server.fail:
            self.send_error(503)
            return
        body = json.dumps(
            {"info": {"title": server.title, "version": server.version}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(autouse=True)
def no_process_exit(service_env):
    """run() в этих тестах не завершает процесс pytest."""
    return service_env


@pytest.fixture
def evtlog(monkeypatch):
    """Журнал событий с поддельным win32evtlog на любой ОС."""
    module = types.ModuleType("win32evtlog")
    module.EVENTLOG_ERROR_TYPE = 1
    module.EVENTLOG_WARNING_TYPE = 2
    module.EVENTLOG_INFORMATION_TYPE = 4
    module.RegisterEventSource = mock.Mock(return_value="source")
    module.DeregisterEventSource = mock.Mock()
    module.ReportEvent = mock.Mock()
    monkeypatch.setitem(sys.modules, "win32evtlog", module)
    monkeypatch.setattr(logging_setup, "_windows", lambda: True)
    monkeypatch.setattr(logging_setup, "event_source_registered", lambda: True)
    return module


def event_ids(evtlog):
    flush_event_logs()
    return [call.args[3] for call in evtlog.ReportEvent.call_args_list]


def event_texts(evtlog, event_id):
    flush_event_logs()
    return [
        call.args[5][0]
        for call in evtlog.ReportEvent.call_args_list
        if call.args[3] == event_id
    ]


@pytest.fixture
def files_dir(tmp_path):
    folder = tmp_path / "files"
    reload_app(files_directory=str(folder))
    return folder


@pytest.fixture
def port(monkeypatch, files_dir):  # noqa: ARG001 - директория для create_app
    """Свободный порт вместо 8000, короткие опросы и паузы."""
    number = free_port()
    monkeypatch.setattr(runner, "HOST", "127.0.0.1")
    monkeypatch.setattr(runner, "PORT", number)
    monkeypatch.setattr(runner, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(instance_check, "PROBE_SECONDS", 0.3)
    monkeypatch.setattr(instance_check, "BIND_PROBE_SECONDS", 0.3)
    monkeypatch.setattr(instance_check, "REQUEST_TIMEOUT", 0.2)
    return number


@pytest.fixture
def fake_serve(monkeypatch):
    """InstanceServer.run без Uvicorn (по умолчанию — старт и стоп)."""
    serve = mock.Mock(side_effect=start_then_stop)

    def run(server, sockets=None):
        return serve(server, sockets)

    monkeypatch.setattr(runner.InstanceServer, "run", run)
    return serve


@pytest.fixture
def unreachable_log(monkeypatch):
    """Файл журнала недоступен: 1004 в буфере стартовых событий."""
    monkeypatch.setattr(
        logging_setup, "log_file_path", mock.Mock(side_effect=OSError("denied"))
    )


class TestStart:
    """Собственный запуск: буфер, 1000, директория."""

    def test_start_and_stop(self, service_env, evtlog, port, fake_serve, files_dir):
        """Проверить: 1000 после старта слушателя, 1001 при остановке,
        код 0, директория создана, Uvicorn на заданном адресе.
        """
        assert runner.run() == 0

        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1000, 1001]
        [started] = event_texts(evtlog, 1000)
        assert f"версия {app.__version__}" in started
        assert f"директория {files_dir}" in started
        assert "запуск из консоли" in started
        [stopped] = event_texts(evtlog, 1001)
        assert stopped == "app.runner: Сервис остановлен: тест"
        [(server, _)] = [c.args for c in fake_serve.call_args_list]
        assert (server.config.host, server.config.port) == ("127.0.0.1", port)
        assert server.config.log_config is None
        assert server.config.timeout_graceful_shutdown == 10
        deadline = time.monotonic() + 5
        while not files_dir.is_dir() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert files_dir.is_dir()

    @pytest.mark.usefixtures("files_dir")
    def test_listens_on_all_interfaces_port_8000(self, fake_serve, monkeypatch):
        """Проверить: без подмен Uvicorn слушает 0.0.0.0:8000 —
        фронтенд с другой машины достучится до сервиса.
        """
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: False)
        assert runner.run() == 0
        [(server, _)] = [c.args for c in fake_serve.call_args_list]
        assert (server.config.host, server.config.port) == ("0.0.0.0", 8000)

    @pytest.mark.usefixtures("port")
    def test_startup_message_error(self, evtlog, monkeypatch):
        """Проверить с настоящим Uvicorn: ошибка текста 1000 не роняет
        запуск — 1000 с версией и причиной, сервис работает.
        """

        def on_started(service):
            started(service)
            service.request_stop("тест")

        started = runner.Service.on_started
        monkeypatch.setattr(runner.Service, "on_started", on_started)
        monkeypatch.setattr(
            runner, "startup_message", mock.Mock(side_effect=OSError("нет exe"))
        )
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 1001]
        [text] = event_texts(evtlog, 1000)
        assert f"версия {app.__version__}" in text
        assert "нет exe" in text

    @pytest.mark.usefixtures("port", "fake_serve", "unreachable_log")
    def test_buffer_sent_on_start(self, evtlog):
        """Проверить: 1004 до старта приходит после него, перед 1000."""
        assert runner.run() == 0
        assert event_ids(evtlog) == [1004, 1000, 1001]

    @pytest.mark.usefixtures("port")
    def test_real_uvicorn_start(self, evtlog, monkeypatch):
        """Проверить с настоящим Uvicorn: сервис отвечает, а 1000 —
        после привязки к порту.
        """
        answers = []

        def ask_and_stop(service):
            response = httpx.get(f"http://127.0.0.1:{runner.PORT}/openapi.json")
            answers.append(response.json()["info"]["title"])
            service.request_stop("тест")

        def on_started(service):
            original(service)
            # Не в цикле событий Uvicorn: он должен ответить на запрос
            threading.Thread(target=ask_and_stop, args=(service,)).start()

        original = runner.Service.on_started
        monkeypatch.setattr(runner.Service, "on_started", on_started)

        assert runner.run() == 0
        assert answers == ["File Picker API"]
        assert event_ids(evtlog) == [1000, 1001]

    @pytest.mark.usefixtures("port")
    def test_hung_report_event(self, evtlog, monkeypatch):
        """Проверить с настоящим Uvicorn: зависший ReportEvent не
        мешает старту — сервис отвечает, остановка завершается в срок,
        а события дописываются, когда журнал событий ответит.
        """
        monkeypatch.setattr(logging_setup, "EVENT_FLUSH_SECONDS", 0.1)
        release = threading.Event()
        evtlog.ReportEvent.side_effect = lambda *_: release.wait(10)
        answers = []

        def ask_and_stop(service):
            response = httpx.get(f"http://127.0.0.1:{runner.PORT}/openapi.json")
            answers.append(response.json()["info"]["title"])
            service.request_stop("тест")

        def on_started(service):
            original(service)
            threading.Thread(target=ask_and_stop, args=(service,)).start()

        original = runner.Service.on_started
        monkeypatch.setattr(runner.Service, "on_started", on_started)
        try:
            assert runner.run() == 0
            assert answers == ["File Picker API"]
            assert event_ids(evtlog) == [1000]
        finally:
            release.set()
        wait_for(lambda: event_ids(evtlog) == [1000, 1001])

    @pytest.mark.usefixtures("files_dir")
    def test_directory_not_created(self, evtlog):
        """Проверить: ошибка mkdir — 2004 с ошибкой, без исключения."""
        logging_setup.setup_logging(console=False)
        error = OSError("network path not found")
        with mock.patch("pathlib.Path.mkdir", side_effect=error):
            runner.ensure_files_directory()
        logging_setup.send_startup_events()
        [text] = event_texts(evtlog, 2004)
        assert "Не удалось создать директорию с файлами" in text
        assert "network path not found" in text


class TestSecondInstance:
    """Порт 8000 занят: File Picker API, другая программа, гонка."""

    @pytest.mark.usefixtures("unreachable_log")
    def test_already_running(self, service_env, evtlog, port, caplog):
        """Проверить: работающий File Picker API — код 0, строка в
        журнале и одно 1005, буфер (1004) отброшен.
        """
        server = OpenApiServer(port)
        try:
            with mock.patch.object(runner.InstanceServer, "run") as serve:
                assert runner.run() == 0
        finally:
            server.close()

        serve.assert_not_called()
        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1005]
        [text] = event_texts(evtlog, 1005)
        assert "Сервис уже работает в другом процессе" in text
        assert f"версия {app.__version__}" in text
        if sys.platform == "win32":
            assert f"PID {os.getpid()}" in text
        else:
            assert "PID" not in text
        flush_event_logs()
        assert evtlog.ReportEvent.call_args.args[1] == 4
        assert "уже работает" in caplog.text

    def test_other_version(self, service_env, evtlog, port):
        """Проверить: другая версия — 1006 (предупреждение)."""
        server = OpenApiServer(port, version="0.0.1-other")
        try:
            assert runner.run() == 0
        finally:
            server.close()
        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1006]
        [text] = event_texts(evtlog, 1006)
        assert "версия 0.0.1-other" in text
        assert f"версия этого запуска {app.__version__}" in text
        flush_event_logs()
        assert evtlog.ReportEvent.call_args.args[1] == 2

    def test_answers_from_third_request(self, evtlog, port):
        """Проверить: первый экземпляр ещё запускается — всё равно 0."""
        server = OpenApiServer(port, fail=2)
        try:
            assert runner.run() == 0
        finally:
            server.close()
        assert server.requests == 3
        assert event_ids(evtlog) == [1005]

    @pytest.mark.usefixtures("fake_serve")
    def test_port_freed_during_probe(self, evtlog, port, monkeypatch):
        """Проверить: порт освободился во время опроса — запуск сразу,
        без 1002.
        """
        sock = blocker(port)
        monkeypatch.setattr(instance_check, "PROBE_SECONDS", 30)
        threading.Timer(0.3, sock.close).start()
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 1001]

    @pytest.mark.usefixtures("unreachable_log")
    def test_stop_during_probe(self, service_env, evtlog, port, monkeypatch):
        """Проверить: остановка во время опроса — 1004 и 1001, сервер
        не создаётся, 1000 и 1002 нет.
        """
        sock = blocker(port)
        monkeypatch.setattr(instance_check, "PROBE_SECONDS", 30)
        threading.Timer(0.3, service_env.stop).start()
        try:
            with mock.patch.object(runner, "InstanceServer") as server_class:
                assert runner.run() == 0
        finally:
            sock.close()
        server_class.assert_not_called()
        assert event_ids(evtlog) == [1004, 1001]

    @pytest.mark.usefixtures("unreachable_log")
    def test_race_for_port(self, service_env, evtlog, port, monkeypatch, tmp_path):
        """Проверить: порт свободен при проверке, но занят File Picker
        API к привязке — код 0 и только 1005, даже при 1004 и 2004.
        """
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("")
        reload_app(files_directory=str(not_a_dir / "files"))
        real = instance_check.port_in_use
        calls = []

        def port_in_use(host, number):
            calls.append(number)
            return len(calls) > 1 and real(host, number)

        monkeypatch.setattr(instance_check, "port_in_use", port_in_use)
        server = OpenApiServer(port)
        try:
            assert runner.run() == 0
        finally:
            server.close()
        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1005]

    def test_stop_wins_over_second_instance(self, service_env, evtlog, monkeypatch):
        """Проверить: остановка до записи исхода — 1001 без 1005."""

        def wait_for_port(*_args):
            service_env.stop()
            return ProbeResult(Probe.FILE_PICKER, app.__version__)

        monkeypatch.setattr(instance_check, "wait_for_port", wait_for_port)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1001]


class TestServerErrors:
    """Ошибки HTTP-сервера: 1002, повторы, ошибка настроек."""

    def test_port_taken_by_other_program(self, service_env, evtlog, port):
        """Проверить с настоящим Uvicorn: порт занят не File Picker
        API —
        напоминание о повторах, четыре 1002 с текстом Uvicorn и номером
        попытки, ни одного 4000 и 3001, код 1.
        """
        event_tracking.admit(3000, "k", "ошибка доступа к файлу")
        event_tracking.admit(3000, "k", "ошибка доступа к файлу")
        sock = blocker(port)
        try:
            assert runner.run() == 1
        finally:
            sock.close()

        assert service_env.exits == [1]
        assert event_ids(evtlog) == [3000, 1002, 1002, 1002, 1002]
        texts = event_texts(evtlog, 1002)
        for number, text in enumerate(texts, start=1):
            assert f"попытка {number} из 4" in text
            assert "error while attempting to bind" in text
        assert "следующая через 0 с" in texts[0]
        assert "попытки исчерпаны" in texts[3]

    def test_port_freed_for_second_attempt(self, evtlog, port, monkeypatch):
        """Проверить: порт освободился ко второй попытке — сервис
        работает.
        """
        sock = blocker(port)
        report = runner.Service._report_failure  # noqa: SLF001 - обёртка

        def report_and_free(service, *args):
            report(service, *args)
            sock.close()

        def on_started(service):
            started(service)
            service.request_stop("тест")

        started = runner.Service.on_started
        monkeypatch.setattr(runner.Service, "_report_failure", report_and_free)
        monkeypatch.setattr(runner.Service, "on_started", on_started)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1002, 1000, 1001]

    def test_file_picker_between_attempts(self, service_env, evtlog, port, monkeypatch):
        """Проверить: File Picker API занял порт между попытками —
        код 0,
        1002 первой попытки и затем 1005, без 1001.
        """
        sock = blocker(port)
        servers = []
        report = runner.Service._report_failure  # noqa: SLF001 - обёртка

        def report_and_replace(service, *args):
            report(service, *args)
            sock.close()
            servers.append(OpenApiServer(port))

        monkeypatch.setattr(runner.Service, "_report_failure", report_and_replace)
        try:
            assert runner.run() == 0
        finally:
            for server in servers:
                server.close()
        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1002, 1005]

    def test_failed_lifespan(self, evtlog, port, monkeypatch):  # noqa: ARG002
        """Проверить с настоящим Uvicorn: неудачный lifespan — 1000 нет,
        1002 с трассировкой, код 1.
        """

        async def lifespan(_app):
            msg = "lifespan не поднялся"
            raise RuntimeError(msg)
            yield

        monkeypatch.setattr("app.api.create_app", lambda: FastAPI(lifespan=lifespan))
        assert runner.run() == 1
        assert event_ids(evtlog) == [1002] * 4
        assert "lifespan не поднялся" in event_texts(evtlog, 1002)[0]

    @pytest.mark.usefixtures("port")
    def test_server_returns_without_stop(self, evtlog, monkeypatch):
        """Проверить: возврат из run() без остановки — тоже ошибка."""
        monkeypatch.setattr(runner.InstanceServer, "run", lambda *_: None)
        assert runner.run() == 1
        texts = event_texts(evtlog, 1002)
        assert len(texts) == 4
        assert all("без запроса остановки" in text for text in texts)

    @pytest.mark.usefixtures("port")
    def test_uvicorn_exception(self, evtlog, monkeypatch):
        """Проверить: исключение Uvicorn — 1002 с ним и трассировкой."""

        def serve(_server, _sockets=None):
            msg = "сбой Uvicorn"
            raise RuntimeError(msg)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 1
        texts = event_texts(evtlog, 1002)
        assert len(texts) == 4
        assert "RuntimeError: сбой Uvicorn" in texts[0]
        assert "Traceback" in texts[0]

    @pytest.mark.usefixtures("port")
    def test_long_run_resets_attempts(self, evtlog, monkeypatch, clock):
        """Проверить: работа дольше часа сбрасывает счётчик попыток."""
        monkeypatch.setattr(runner, "clock", clock)
        runs = []

        def serve(_server, _sockets=None):
            runs.append(1)
            if len(runs) == 2:
                clock.advance(runner.RESET_AFTER_SECONDS)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 1
        texts = event_texts(evtlog, 1002)
        numbers = [text.split("попытка ")[1].split(" ")[0] for text in texts]
        assert numbers == ["1", "1", "2", "3", "4"]

    @pytest.mark.usefixtures("port")
    def test_stop_during_retry_wait(self, service_env, evtlog, monkeypatch):
        """Проверить: остановка прерывает паузу между попытками."""
        monkeypatch.setattr(runner, "RETRY_DELAYS", (60, 60, 60))
        monkeypatch.setattr(runner.InstanceServer, "run", lambda *_: None)
        report = runner.Service._report_failure  # noqa: SLF001 - обёртка

        def report_then_stop(service, *args):
            report(service, *args)
            # Сигнал приходит уже во время паузы, после записи 1002
            threading.Timer(0.2, service_env.stop).start()

        monkeypatch.setattr(runner.Service, "_report_failure", report_then_stop)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1002, 1001]

    def test_worker_returns_without_outcome(self, service_env, evtlog, monkeypatch):
        """Проверить: рабочий поток вышел без исхода — 1002 о нём и
        код 1, а не тихий выход.
        """
        monkeypatch.setattr(runner.Service, "_work", lambda _self: None)
        assert runner.run() == 1
        assert service_env.exits == [1]
        [text] = event_texts(evtlog, 1002)
        assert "Рабочий поток завершился без исхода" in text

    # Исключение нарочно выходит из рабочего потока
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_failure_while_reporting(self, service_env, monkeypatch):
        """Проверить: сбой при записи исхода не оставляет главный поток
        ждать вечно — код 1.
        """
        monkeypatch.setattr(
            instance_check, "wait_for_port", mock.Mock(side_effect=RuntimeError("x"))
        )
        monkeypatch.setattr(
            runner.Service,
            "_report_failure",
            mock.Mock(side_effect=RuntimeError("журнал")),
        )
        assert runner.run() == 1
        assert service_env.exits == [1]
        # Предупреждение о потоке — в этом тесте, а не в следующем
        for thread in threading.enumerate():
            if thread.name == "service-worker":
                thread.join(5)

    @pytest.mark.usefixtures("port")
    def test_settings_error(self, service_env, evtlog, files_dir, monkeypatch):
        """Проверить: FILES_DIRECTORY в .env — одно 1002 и код 1 без
        повторов и без 1001, сервер не создаётся, директория тоже.
        """
        from app.settings import get_settings

        monkeypatch.chdir(files_dir.parent)
        # reload_app уже прочитал настройки без .env
        get_settings.cache_clear()
        (files_dir.parent / ".env").write_text(
            "FILES_DIRECTORY=D:\\x\n", encoding="utf-8"
        )
        with mock.patch.object(runner, "InstanceServer") as server_class:
            assert runner.run() == 1
        server_class.assert_not_called()
        assert not files_dir.exists()
        assert service_env.exits == [1]
        assert event_ids(evtlog) == [1002]
        [text] = event_texts(evtlog, 1002)
        assert "Ошибка настроек, сервис не запущен" in text
        assert "files_directory" in text.lower()
        assert "Traceback" in text

    def test_unexpected_worker_error(self, evtlog, monkeypatch):
        """Проверить: ошибка самого рабочего потока — 1002 и код 1."""
        monkeypatch.setattr(
            instance_check, "wait_for_port", mock.Mock(side_effect=RuntimeError("x"))
        )
        assert runner.run() == 1
        [text] = event_texts(evtlog, 1002)
        assert "Непредвиденная ошибка запуска сервиса" in text

    def test_error_after_stop_goes_to_1001(self, service_env, evtlog, monkeypatch):
        """Проверить: сбой после запроса остановки — пометка в 1001."""

        def wait_for_port(*_args):
            service_env.stop()
            return fail()

        def fail():
            msg = "сбой"
            raise RuntimeError(msg)

        monkeypatch.setattr(instance_check, "wait_for_port", wait_for_port)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1001]
        [text] = event_texts(evtlog, 1001)
        assert "Ошибка при остановке HTTP-сервера: RuntimeError: сбой" in text

    @pytest.mark.usefixtures("port")
    def test_stop_during_bind_probe(self, service_env, evtlog, monkeypatch):
        """Проверить: остановка во время короткой проверки — 1001."""
        results = iter(
            [ProbeResult(Probe.FREE), ProbeResult(Probe.STOPPED)],
        )

        def wait_for_port(*_args):
            result = next(results)
            if result.status is Probe.STOPPED:
                service_env.stop()
            return result

        def serve(server, _sockets=None):
            server.errors.address_in_use = True
            raise SystemExit(3)

        monkeypatch.setattr(instance_check, "wait_for_port", wait_for_port)
        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1001]


class TestFinalEventOrder:
    """Завершающее событие — после событий о папке, ждущих ответа."""

    @pytest.fixture
    def slow_first_report(self, evtlog):
        """Журнал событий настроен, первое событие пишется медленно."""
        logging_setup.setup_logging(console=False)
        logging_setup.send_startup_events()
        calls = []

        def report(*args):
            calls.append(args[3])
            if len(calls) == 1:
                time.sleep(0.3)

        evtlog.ReportEvent.side_effect = report
        return calls

    @pytest.mark.usefixtures("clock")
    def test_stop_after_delayed_ack(self, slow_first_report):
        """Проверить: подтверждение 2000 запаздывает — итоговое 2002
        ложится раньше 1001.
        """
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        service = runner.Service(console=False)
        service.stop_reason = "тест"
        service.finalize()
        flush_event_logs()

        assert slow_first_report == [2000, 2002, 1001]

    @pytest.mark.usefixtures("clock")
    def test_failure_after_delayed_ack(self, slow_first_report):
        """Проверить: то же перед 1002."""
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        service = runner.Service(console=False)
        service._report_failure("сбой", None)  # noqa: SLF001
        flush_event_logs()

        assert slow_first_report == [2000, 2002, 1002]

    @pytest.mark.usefixtures("clock")
    def test_failed_write_counted_in_summary(self, evtlog):
        """Проверить: первое 3000 ещё пишется при остановке и не
        записывается — итог учитывает его случай вместе с повтором, и
        напоминание ложится раньше 1001.
        """
        logging_setup.setup_logging(console=False)
        logging_setup.send_startup_events()
        calls = []

        def report(*args):
            calls.append((args[3], args[5][0]))
            if len(calls) == 1:
                time.sleep(0.3)
                failure = "full"
                raise OSError(failure)

        evtlog.ReportEvent.side_effect = report
        for _ in range(2):
            event_tracking.track(event_tracking.EventId.FILE_ACCESS_ERROR, "k", "отказ")
        service = runner.Service(console=False)
        service.stop_reason = "тест"
        service.finalize()
        flush_event_logs()

        assert [event_id for event_id, _ in calls] == [3000, 3000, 1001]
        assert calls[1][1].endswith("Повторялось ещё 2 раз с прошлой записи: отказ")

    def test_wait_leaves_exit_reserve(self, monkeypatch):
        """Проверить: ожидание журнала событий оставляет запас срока
        сторожа на сброс журнала.
        """
        waits = []
        monkeypatch.setattr(runner, "wait_event_log", waits.append)
        monkeypatch.setattr(runner, "clock", lambda: 100.0)
        service = runner.Service(console=False)
        service.exit_deadline = 100.0 + runner.EXIT_RESERVE_SECONDS + 1
        service._wait_event_log()  # noqa: SLF001
        service.exit_deadline = 100.0
        service._wait_event_log()  # noqa: SLF001

        assert waits == [1, 0]

    def test_urgent_waits_until_stop_deadline(self, monkeypatch):
        """Проверить: при срочной остановке журнал событий ждём не
        дольше срока ожидания потоков — у Windows на всё 5 секунд.
        """
        waits = []
        monkeypatch.setattr(runner, "wait_event_log", waits.append)
        monkeypatch.setattr(runner, "clock", lambda: 100.0)
        service = runner.Service(console=False)
        service.urgent = True
        service.stop_deadline = 101.0
        service._wait_event_log()  # noqa: SLF001
        service.stop_deadline = 99.0
        service._wait_event_log()  # noqa: SLF001

        assert waits == [1, 0]


class TestStop:
    """Обычная остановка: 1001 один раз, напоминания, зависания."""

    @pytest.mark.usefixtures("port")
    def test_two_signals_one_stop(self, evtlog, monkeypatch):
        """Проверить: второй сигнал остановку не повторяет."""

        def serve(server, _sockets=None):
            start_then_stop(server)
            server.service.request_stop("второй")

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_texts(evtlog, 1001) == ["app.runner: Сервис остановлен: тест"]

    @pytest.mark.usefixtures("port")
    def test_repeat_during_shutdown(self, evtlog, monkeypatch):
        """Проверить: повтор ошибки во время мягкой остановки — в
        напоминании до 1001.
        """

        def serve(server, _sockets=None):
            start_then_stop(server)
            for _ in range(3):
                event_tracking.track(3000, "k", "ошибка доступа к файлу")

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 3000, 3000, 1001]
        assert "Повторялось ещё 2 раз" in event_texts(evtlog, 3000)[1]

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_hung_directory(self, evtlog, monkeypatch):
        """Проверить: зависшее создание директории при остановленном
        Uvicorn — пометка в единственном 1001.
        """
        release = threading.Event()
        monkeypatch.setattr(runner, "ensure_files_directory", release.wait)
        try:
            assert runner.run() == 0
        finally:
            release.set()
        [text] = event_texts(evtlog, 1001)
        assert "Обращение к сетевой папке не завершилось" in text

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_directory_just_started(self, evtlog, monkeypatch):
        """Проверить: mkdir, закончившийся за доли секунды после
        сигнала, — не зависание, пометки в 1001 нет.
        """
        monkeypatch.setattr(runner, "ensure_files_directory", lambda: time.sleep(0.3))
        assert runner.run() == 0
        assert event_texts(evtlog, 1001) == ["app.runner: Сервис остановлен: тест"]

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_watchdog_exits_when_logging_hangs(self, monkeypatch):
        """Проверить: финализация зависла (замок обработчика журнала) —
        сторож всё равно завершает процесс в срок.
        """
        released = threading.Event()
        exits = []

        def exit_process(code):
            exits.append(code)
            released.set()

        monkeypatch.setattr(runner, "_exit", exit_process)
        monkeypatch.setattr(runner, "EXIT_DEADLINE_SECONDS", 0.2)
        monkeypatch.setattr(runner.Service, "finalize", lambda _self: released.wait(10))
        assert runner.run() == 0
        # Первый выход — от сторожа, пока финализация ещё висела
        assert exits[0] == 0
        assert len(exits) == 2

    def test_watchdog_exits_when_failure_logging_hangs(self, monkeypatch):
        """Проверить: запись 1002 зависла — сторож завершает процесс
        с кодом 1, хотя события сбоя так и не записаны.
        """
        exited = threading.Event()
        exits = []

        def exit_process(code):
            exits.append(code)
            exited.set()

        monkeypatch.setattr(runner, "_exit", exit_process)
        monkeypatch.setattr(runner, "EXIT_DEADLINE_SECONDS", 0.2)
        monkeypatch.setattr(stop_signals, "install", mock.Mock())
        monkeypatch.setattr(
            instance_check, "wait_for_port", mock.Mock(side_effect=RuntimeError("x"))
        )
        monkeypatch.setattr(
            runner.Service, "_report_failure", lambda *_: exited.wait(10)
        )
        assert runner.run() == 1
        assert exits == [1, 1]

    @pytest.mark.usefixtures("port", "service_env")
    def test_retry_delay_includes_report(self, monkeypatch):
        """Проверить: время записи 1002 входит в паузу перед повтором,
        а не удлиняет её.
        """
        now = [0.0]
        monkeypatch.setattr(runner, "clock", lambda: now[0])
        monkeypatch.setattr(runner, "RETRY_DELAYS", (10, 30, 60))
        attempts, waits = [], []

        def serve(server, _sockets=None):
            attempts.append(server)
            if len(attempts) == 1:
                raise SystemExit(1)
            start_then_stop(server)

        def report(*_args):
            # Запись 1002 заняла 4 секунды
            now[0] += 4

        class PauseEvent(threading.Event):
            """stop_requested: пауза перед повтором записывается."""

            def wait(self, timeout=None):
                if timeout is not None and not self.is_set():
                    waits.append(timeout)
                    return False
                return super().wait(timeout)

        real_init = runner.Service.__init__

        def init(service, **kwargs):
            real_init(service, **kwargs)
            service.stop_requested = PauseEvent()

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        monkeypatch.setattr(runner.Service, "_report_failure", report)
        monkeypatch.setattr(runner.Service, "__init__", init)

        assert runner.run() == 0
        assert waits == [6]

    @pytest.mark.usefixtures("port", "service_env")
    def test_hung_retry_report(self, monkeypatch):
        """Проверить: зависшая запись 1002 перед повтором не мешает
        следующей попытке.
        """
        monkeypatch.setattr(runner, "REPORT_WAIT_SECONDS", 0.1)
        release = threading.Event()
        attempts = []

        def serve(server, _sockets=None):
            attempts.append(server)
            if len(attempts) == 1:
                raise SystemExit(1)
            start_then_stop(server)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        monkeypatch.setattr(
            runner.Service, "_report_failure", lambda *_: release.wait(10)
        )
        try:
            assert runner.run() == 0
        finally:
            release.set()
        assert len(attempts) == 2

    @pytest.mark.usefixtures("port")
    def test_hung_retry_report_noted_in_start(self, monkeypatch, caplog):
        """Проверить: 1002 прошлой попытки ещё пишется — в 1000 пометка,
        что оно может появиться в журнале позже.
        """
        monkeypatch.setattr(runner, "REPORT_WAIT_SECONDS", 0.1)
        release = threading.Event()
        attempts = []

        def serve(server, _sockets=None):
            attempts.append(server)
            if len(attempts) == 1:
                raise SystemExit(1)
            start_then_stop(server)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        monkeypatch.setattr(
            runner.Service, "_report_failure", lambda *_: release.wait(10)
        )
        try:
            assert runner.run() == 0
        finally:
            release.set()
        [started] = [
            r.getMessage()
            for r in caplog.records
            if getattr(r, "event_id", None) == event_tracking.EventId.SERVICE_STARTED
        ]
        assert started.endswith(
            "Запись о прошлой неудачной попытке (1002) ещё не завершена и "
            "может появиться в журнале позже"
        )

    def test_retry_report_error(self, monkeypatch):
        """Проверить: ошибка записи 1002 перед повтором — сбой запуска
        с кодом 1, как и раньше, а не трассировка в stderr из потока.
        """
        attempts, messages = [], []
        original = runner.Service._report_failure  # noqa: SLF001 - подмена

        def serve(server, _sockets=None):
            attempts.append(server)
            raise SystemExit(1)

        def report(service, message, error):
            messages.append(message)
            if len(messages) == 1:
                failure = "отказ записи"
                raise RuntimeError(failure)
            original(service, message, error)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        monkeypatch.setattr(runner.Service, "_report_failure", report)
        assert runner.run() == 1
        assert len(attempts) == 1
        assert messages[1] == "Непредвиденная ошибка запуска сервиса"

    @pytest.mark.usefixtures("port")
    def test_hung_worker(self, service_env, evtlog, monkeypatch):
        """Проверить: рабочий поток не завершился за срок — пометка в
        1001 и выход без его ожидания.
        """
        release = threading.Event()
        monkeypatch.setattr(runner, "STOP_WAIT_SECONDS", 0.2)

        def serve(server, _sockets=None):
            start_then_stop(server)
            release.wait()

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        try:
            assert runner.run() == 0
        finally:
            release.set()
        assert service_env.exits == [0]
        [text] = event_texts(evtlog, 1001)
        assert "не завершилось" in text

    @pytest.mark.usefixtures("port", "unreachable_log")
    def test_stop_before_server(self, service_env, evtlog, monkeypatch):
        """Проверить: остановка до создания сервера — сервер не
        создаётся, накопленное и 1001, без 1000 и 1002.
        """
        import app.api

        create_app = app.api.create_app

        def stop_then_create():
            service_env.stop()
            return create_app()

        monkeypatch.setattr(app.api, "create_app", stop_then_create)
        with mock.patch.object(runner.InstanceServer, "run") as serve:
            assert runner.run() == 0
        serve.assert_not_called()
        assert event_ids(evtlog) == [1004, 1001]

    @pytest.mark.usefixtures("port")
    def test_stop_before_listener(self, evtlog, monkeypatch):
        """Проверить: остановка до старта слушателя — 1000 нет."""

        def serve(server, _sockets=None):
            server.service.request_stop("тест")
            server.started = True
            server.service.on_started()

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1001]

    @pytest.mark.usefixtures("port")
    def test_error_during_shutdown(self, evtlog, monkeypatch):
        """Проверить: исключение при мягкой остановке — одно 1001 с
        пометкой, код 0.
        """

        def serve(server, _sockets=None):
            start_then_stop(server)
            msg = "сбой при остановке"
            raise RuntimeError(msg)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        [text] = event_texts(evtlog, 1001)
        assert "Ошибка при остановке HTTP-сервера: RuntimeError" in text

    @pytest.mark.usefixtures("port")
    def test_uvicorn_exit_during_shutdown(self, evtlog, monkeypatch):
        """Проверить: SystemExit при остановке — код Uvicorn в 1001."""

        def serve(server, _sockets=None):
            start_then_stop(server)
            raise SystemExit(3)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        [text] = event_texts(evtlog, 1001)
        assert "Uvicorn завершился с кодом 3" in text

    @pytest.mark.usefixtures("port")
    def test_real_uvicorn_stops(self, service_env, evtlog, monkeypatch):
        """Проверить с настоящим Uvicorn: остановка по should_exit."""

        def on_started(service):
            started(service)
            service_env.stop("Ctrl+C в консоли")

        started = runner.Service.on_started
        monkeypatch.setattr(runner.Service, "on_started", on_started)
        assert runner.run() == 0
        assert event_texts(evtlog, 1001) == [
            "app.runner: Сервис остановлен: Ctrl+C в консоли"
        ]


def urgent_in_thread(service_env, reason):
    """Срочный сигнал из своего потока, как от Windows.

    Возвращает поток и список, где отмечено, сколько выходов процесса
    было к моменту возврата из обработчика.
    """
    exits_on_return = []

    def handler():
        service_env.urgent(reason)
        exits_on_return.append(len(service_env.exits))

    thread = threading.Thread(target=handler, daemon=True)
    thread.start()
    return thread, exits_on_return


class TestUrgentStop:
    """Срочная остановка: закрытие консоли и завершение сеанса."""

    @pytest.mark.parametrize(
        "reason",
        [
            "закрытие консоли",
            "обновление или удаление программы",
            "выход пользователя",
            "выключение Windows",
        ],
    )
    @pytest.mark.usefixtures("port")
    def test_reason_and_reminders(self, service_env, evtlog, monkeypatch, reason):
        """Проверить: накопленные повторы — напоминание, затем 1001 с
        причиной; обработчик возвращается только после выхода процесса.
        """
        handler = {}
        flush = mock.Mock(wraps=event_tracking.flush)
        monkeypatch.setattr(event_tracking, "flush", flush)

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            for _ in range(6):
                event_tracking.track(3000, "k", "ошибка доступа к файлу")
            # Срочная остановка поток директории не ждёт, а только
            # проверяет: без этого 1001 изредка получает пометку о
            # зависшем обращении
            wait_for(lambda: server.service.directory_finished)
            handler["thread"], handler["exits"] = urgent_in_thread(service_env, reason)
            wait_for(server.service.stop_requested.is_set)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        handler["thread"].join(5)

        assert service_env.exits == [0]
        assert handler["exits"] == [1]
        assert event_ids(evtlog) == [1000, 3000, 3000, 1001]
        assert "Повторялось ещё 5 раз" in event_texts(evtlog, 3000)[1]
        assert event_texts(evtlog, 1001) == [f"app.runner: Сервис остановлен: {reason}"]
        # Такт учёта повторов не ждём: срок Windows — 5 секунд
        flush.assert_called_once_with(wait=0)

    @pytest.mark.usefixtures("port")
    def test_waits_from_urgent_signal(self, service_env, evtlog, monkeypatch):
        """Проверить: зависшие рабочий поток и создание директории ждём
        срок срочной остановки от прихода сигнала — не 20 секунд и не
        дольше ещё на ожидание директории; пометка в 1001.
        """
        assert runner.URGENT_WAIT_SECONDS == 3
        monkeypatch.setattr(runner, "URGENT_WAIT_SECONDS", 0.5)
        monkeypatch.setattr(runner, "STOP_WAIT_SECONDS", 60)
        monkeypatch.setattr(runner, "DIRECTORY_WAIT_SECONDS", 30)
        release = threading.Event()
        monkeypatch.setattr(runner, "ensure_files_directory", release.wait)
        handler = {}

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            handler["signal"] = time.monotonic()
            handler["thread"], _ = urgent_in_thread(service_env, "выход пользователя")
            release.wait()

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        try:
            assert runner.run() == 0
            elapsed = time.monotonic() - handler["signal"]
        finally:
            release.set()
        assert 0.5 <= elapsed < 2
        handler["thread"].join(5)
        assert service_env.exits == [0]
        [text] = event_texts(evtlog, 1001)
        assert "Сервис остановлен: выход пользователя" in text
        assert "не завершилось" in text

    @pytest.mark.usefixtures("port")
    def test_directory_not_waited(self, service_env, evtlog, monkeypatch):
        """Проверить: при срочной остановке поток директории не ждём
        даже секунду — mkdir, идущий в момент проверки, отмечен в 1001.
        """
        release = threading.Event()
        monkeypatch.setattr(runner, "ensure_files_directory", release.wait)
        handler = {}

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            handler["thread"], _ = urgent_in_thread(service_env, "закрытие консоли")
            wait_for(server.service.stop_requested.is_set)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        # Иначе mkdir успел бы закончиться за секунду ожидания
        threading.Timer(0.5, release.set).start()
        started = time.monotonic()
        assert runner.run() == 0
        assert time.monotonic() - started < 0.5
        handler["thread"].join(5)
        [text] = event_texts(evtlog, 1001)
        assert "не завершилось" in text

    @pytest.mark.usefixtures("port")
    def test_urgent_during_normal_stop(self, service_env, evtlog, monkeypatch):
        """Проверить: срочный сигнал во время обычной остановки —
        одна остановка с прежней причиной, ожидание сокращается.
        """
        monkeypatch.setattr(runner, "URGENT_WAIT_SECONDS", 0.3)
        monkeypatch.setattr(runner, "STOP_WAIT_SECONDS", 60)
        release = threading.Event()
        handler = {}

        def serve(server, _sockets=None):
            start_then_stop(server)
            # Главный поток уже ждёт рабочий поток в финализации
            time.sleep(0.2)
            handler["thread"], handler["exits"] = urgent_in_thread(
                service_env, "закрытие консоли"
            )
            release.wait()

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        started = time.monotonic()
        try:
            assert runner.run() == 0
        finally:
            release.set()
        assert time.monotonic() - started < 10
        handler["thread"].join(5)
        assert service_env.exits == [0]
        assert handler["exits"] == [1]
        [text] = event_texts(evtlog, 1001)
        assert text.startswith("app.runner: Сервис остановлен: тест.")

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_handler_exits_when_finalization_hangs(self, service_env, monkeypatch):
        """Проверить: срочный сигнал во время зависшей обычной остановки
        — процесс завершает сам обработчик в срок от сигнала, не
        возвращаясь раньше.
        """
        assert runner.URGENT_EXIT_SECONDS == 4.5
        monkeypatch.setattr(runner, "URGENT_EXIT_SECONDS", 0.3)
        released = threading.Event()
        handler = {}

        def finalize(_service):
            handler["signal"] = time.monotonic()
            handler["thread"], handler["exits"] = urgent_in_thread(
                service_env, "выход пользователя"
            )
            released.wait(10)
            handler["elapsed"] = time.monotonic() - handler["signal"]

        def exit_process(code):
            service_env.exits.append(code)
            released.set()

        monkeypatch.setattr(runner.Service, "finalize", finalize)
        monkeypatch.setattr(runner, "_exit", exit_process)
        assert runner.run() == 0
        handler["thread"].join(5)
        # Первый выход — от обработчика, пока финализация висела
        assert service_env.exits == [0, 0]
        assert handler["exits"] == [1]
        # Допуск на шаг часов Windows (около 15 мс)
        assert 0.25 <= handler["elapsed"] < 2

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_handler_exits_during_window_creation(
        self, service_env, fake_window, monkeypatch
    ):
        """Проверить: закрытие консоли, пока главный поток ещё создаёт
        скрытое окно (рабочего потока и сторожа нет), — выход в срок.
        """
        monkeypatch.setattr(runner, "URGENT_EXIT_SECONDS", 0.3)
        monkeypatch.setattr(stop_signals, "install", INSTALL)
        creating = threading.Event()
        release = threading.Event()

        def create_window(*_args):
            creating.set()
            release.wait(10)

        fake_window.win32gui.CreateWindow.side_effect = create_window
        result = {}
        main = threading.Thread(target=lambda: result.update(code=runner.run()))
        main.start()
        try:
            wait_for(creating.is_set)
            [(handler, _add)] = [
                c.args for c in fake_window.win32api.SetConsoleCtrlHandler.mock_calls
            ]
            started = time.monotonic()
            assert handler(stop_signals.CTRL_CLOSE_EVENT) is True
            elapsed = time.monotonic() - started
            assert service_env.exits == [0]
        finally:
            release.set()
            main.join(10)
        # Допуск на шаг часов Windows (около 15 мс)
        assert 0.25 <= elapsed < 2
        # Затем главный поток доходит до остановки сам
        assert result == {"code": 0}

    def test_exits_with_lock_held(self, service_env):
        """Проверить: замок сервиса занят другим потоком — обработчик
        всё равно завершает процесс в срок, а запрос остановки
        выполняется, когда замок освободится.
        """
        service = runner.Service(console=True)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with service.lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            wait_for(held.is_set)
            with mock.patch.object(runner, "URGENT_EXIT_SECONDS", 0.05):
                service.urgent_stop("закрытие консоли")
            assert service_env.exits == [0]
            assert not service.stop_requested.is_set()
        finally:
            release.set()
            holder.join(5)
        wait_for(service.stop_requested.is_set)
        assert service.stop_reason == "закрытие консоли"
        assert service.urgent

    def test_after_failure(self, service_env):
        """Проверить: срочный сигнал после сбоя ничего не останавливает
        и при зависшем выходе завершает процесс с кодом сбоя.
        """
        service = runner.Service(console=True)
        assert service.claim(runner.Outcome.FAILED)
        with mock.patch.object(runner, "URGENT_EXIT_SECONDS", 0.01):
            service.urgent_stop("выход пользователя")
        assert service_env.exits == [1]
        assert service.stop_reason == ""
        assert service.stop_deadline == float("inf")
        assert not service.stop_requested.is_set()


@pytest.fixture
def compiled_exe(monkeypatch, tmp_path):
    """Собранный exe на Windows: путь от GetModuleFileName, поддельные
    регистрация перезапуска и проверка прав.
    """
    from app import permissions, winapi

    exe = tmp_path / "FilePickerAPI" / "FilePickerAPI.exe"
    win32api = types.ModuleType("win32api")
    win32api.GetModuleFileName = mock.Mock(return_value=str(exe))
    win32api.GetUserNameEx = mock.Mock(return_value="BRANCH\\operator")
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    monkeypatch.setattr(runner, "_windows", lambda: True)
    monkeypatch.setattr(runner, "__compiled__", object(), raising=False)
    violation = permissions.Violation(str(exe.parent), permissions.Problem.NULL_DACL)
    fake = types.SimpleNamespace(
        exe=exe,
        violation=violation,
        restart=mock.Mock(),
        check=mock.Mock(return_value=[violation]),
    )
    monkeypatch.setattr(winapi, "register_application_restart", fake.restart)
    monkeypatch.setattr(permissions, "check_build_folder", fake.check)
    monkeypatch.setattr(permissions, "format_violations", lambda v: f"нарушения: {v}")
    return fake


def checks_finished():
    """Поток проверки прав завершился (или не запускался)."""
    return not any(t.name == "check-permissions" for t in threading.enumerate())


class TestCompiledExe:
    """Перезапуск после обновления и проверка прав у собранного exe."""

    @pytest.mark.usefixtures("port")
    def test_restart_and_permissions(self, compiled_exe, evtlog, monkeypatch):
        """Проверить: перезапуск зарегистрирован, права папки exe
        проверены, 1003 — из буфера после старта слушателя.
        """

        def serve(server, _sockets=None):
            wait_for(checks_finished)
            assert evtlog.ReportEvent.call_count == 0
            start_then_stop(server)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0

        compiled_exe.restart.assert_called_once_with()
        compiled_exe.check.assert_called_once_with(compiled_exe.exe.parent)
        assert event_ids(evtlog) == [1003, 1000, 1001]
        [text] = event_texts(evtlog, 1003)
        assert "нарушения" in text
        assert evtlog.ReportEvent.call_args_list[0].args[1] == 2

    @pytest.mark.usefixtures("port")
    def test_after_start(self, compiled_exe, evtlog, monkeypatch):
        """Проверить: проверка, закончившаяся после старта, — 1003
        сразу, после 1000.
        """
        release = threading.Event()

        def check(_folder):
            release.wait(5)
            return [compiled_exe.violation]

        compiled_exe.check.side_effect = check

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            release.set()
            wait_for(checks_finished)
            server.service.request_stop("тест")

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 1003, 1001]

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_no_violations(self, compiled_exe, evtlog):
        compiled_exe.check.return_value = []
        assert runner.run() == 0
        wait_for(checks_finished)
        assert 1003 not in event_ids(evtlog)

    @pytest.mark.usefixtures("port")
    def test_check_fails(self, compiled_exe, evtlog, monkeypatch):
        """Проверить: сбой проверки — тоже 1003, права не проверены."""
        compiled_exe.check.side_effect = RuntimeError("сбой обхода")

        def serve(server, _sockets=None):
            wait_for(checks_finished)
            start_then_stop(server)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        [text] = event_texts(evtlog, 1003)
        assert "Не удалось проверить права на папку сборки: RuntimeError" in text
        assert "сбой обхода" in text

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_restart_fails(self, compiled_exe, evtlog, caplog):
        """Проверить: отказ регистрации — предупреждение, сервис
        работает.
        """
        compiled_exe.restart.side_effect = OSError("HRESULT 0x80070057")
        assert runner.run() == 0
        assert "Не удалось зарегистрировать перезапуск после обновления" in caplog.text
        assert 1000 in event_ids(evtlog)

    @pytest.mark.usefixtures("unreachable_log")
    def test_second_instance_skips_both(self, compiled_exe, evtlog, port):
        """Проверить: второй экземпляр не регистрирует перезапуск и не
        проверяет права.
        """
        server = OpenApiServer(port)
        try:
            assert runner.run() == 0
        finally:
            server.close()
        compiled_exe.restart.assert_not_called()
        compiled_exe.check.assert_not_called()
        assert event_ids(evtlog) == [1005]

    @pytest.mark.usefixtures("unreachable_log")
    def test_race_for_port_with_violations(
        self, compiled_exe, service_env, evtlog, port, monkeypatch
    ):
        """Проверить: порт занял File Picker API к привязке — только
        1005, даже при нарушении прав на папку сборки.
        """
        real = instance_check.port_in_use
        calls = []

        def port_in_use(host, number):
            calls.append(number)
            return len(calls) > 1 and real(host, number)

        monkeypatch.setattr(instance_check, "port_in_use", port_in_use)
        server = OpenApiServer(port)
        try:
            assert runner.run() == 0
        finally:
            server.close()
        wait_for(checks_finished)
        compiled_exe.check.assert_called_once()
        assert service_env.exits == [0]
        assert event_ids(evtlog) == [1005]

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_stop_waits_for_check(self, compiled_exe, evtlog):
        """Проверить: остановка во время обхода папки — 1003 до 1001."""
        release = threading.Event()

        def check(_folder):
            release.wait(5)
            return [compiled_exe.violation]

        compiled_exe.check.side_effect = check
        timer = threading.Timer(0.3, release.set)
        timer.start()
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 1003, 1001]

    @pytest.mark.usefixtures("port", "compiled_exe")
    def test_write_in_progress_before_close(self, evtlog, monkeypatch):
        """Проверить: запись 1003, начатая до закрытия (флаг уже
        проверен), заканчивается раньше 1001, даже если срок ожидания
        проверки истёк.
        """
        monkeypatch.setattr(runner, "PERMISSIONS_WAIT_SECONDS", 0.01)
        go = threading.Event()
        entered = threading.Event()
        warning = runner.logger.warning

        def slow_warning(msg, *args, **kwargs):
            if kwargs.get("extra", {}).get("event_id") == 1003:
                entered.set()
                go.wait(5)
                time.sleep(0.3)
            warning(msg, *args, **kwargs)

        monkeypatch.setattr(runner.logger, "warning", slow_warning)

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            go.set()
            wait_for(entered.is_set)
            server.service.request_stop("тест")

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert event_ids(evtlog) == [1000, 1003, 1001]

    @pytest.mark.usefixtures("port", "fake_serve", "compiled_exe")
    def test_write_longer_than_close_wait(self, evtlog, monkeypatch, caplog):
        """Проверить: запись 1003, начатая до закрытия, но задержанная
        дольше EVENTS_LOCK_SECONDS, — после 1001 только в файл.
        """
        monkeypatch.setattr(runner, "PERMISSIONS_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(runner, "EVENTS_LOCK_SECONDS", 0.05)
        go = threading.Event()
        handle = runner.logger.handle

        def slow_handle(record):
            # Запись уже создана, флаг события в ней задан
            if getattr(record, "event_id", None) == 1003:
                go.wait(5)
            handle(record)

        monkeypatch.setattr(runner.logger, "handle", slow_handle)
        try:
            assert runner.run() == 0
        finally:
            go.set()
        wait_for(checks_finished)
        assert event_ids(evtlog) == [1000, 1001]
        assert "нарушения" in caplog.text

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_late_check_only_to_file(self, compiled_exe, evtlog, monkeypatch, caplog):
        """Проверить: проверка, не закончившаяся к 1001, — после него
        только в файл журнала.
        """
        monkeypatch.setattr(runner, "PERMISSIONS_WAIT_SECONDS", 0.1)
        release = threading.Event()

        def check(_folder):
            release.wait(5)
            return [compiled_exe.violation]

        compiled_exe.check.side_effect = check
        assert runner.run() == 0
        release.set()
        wait_for(checks_finished)
        assert event_ids(evtlog) == [1000, 1001]
        assert "нарушения" in caplog.text

    @pytest.mark.usefixtures("port")
    def test_settings_error_with_violations(
        self, compiled_exe, service_env, evtlog, files_dir, monkeypatch
    ):
        """Проверить: ошибка настроек — права всё равно проверены, 1003
        до 1002.
        """
        from app.settings import get_settings

        monkeypatch.chdir(files_dir.parent)
        get_settings.cache_clear()
        (files_dir.parent / ".env").write_text(
            "FILES_DIRECTORY=D:\\x\n", encoding="utf-8"
        )
        assert runner.run() == 1
        compiled_exe.check.assert_called_once()
        assert service_env.exits == [1]
        assert event_ids(evtlog) == [1003, 1002]

    @pytest.mark.usefixtures("port", "fake_serve")
    def test_source_run_skips_both(self, compiled_exe, monkeypatch):
        """Проверить: `python -m app` (не собранный exe) — ни
        перезапуска, ни проверки прав.
        """
        monkeypatch.delattr(runner, "__compiled__")
        assert runner.run() == 0
        compiled_exe.restart.assert_not_called()
        compiled_exe.check.assert_not_called()


class TestStartupErrors:
    """Перехват записей uvicorn.error до старта слушателя."""

    def test_error_only_to_file(self):
        """Проверить: ERROR помечается «только в файл», текст и
        трассировка сохраняются, ошибка привязки распознаётся.
        """
        errors = runner.StartupErrors()
        error = RuntimeError("сбой")
        exc_info = (RuntimeError, error, None)
        record = logging.makeLogRecord(
            {"levelno": logging.ERROR, "msg": "Ошибка lifespan", "exc_info": exc_info}
        )
        bind = logging.makeLogRecord(
            {"levelno": logging.ERROR, "msg": OSError(10048, "address in use")}
        )
        info = logging.makeLogRecord({"levelno": logging.INFO, "msg": "старт"})
        for item in (record, bind, info):
            assert errors.filter(item)

        assert record.event_log is False
        assert not hasattr(info, "event_log")
        assert errors.messages[0] == "Ошибка lifespan\nRuntimeError: сбой"
        assert errors.address_in_use

    def test_inactive_after_start(self):
        errors = runner.StartupErrors()
        errors.active = False
        record = logging.makeLogRecord({"levelno": logging.ERROR, "msg": "x"})
        assert errors.filter(record)
        assert not hasattr(record, "event_log")
        assert errors.messages == []


class TestStartupMessage:
    """Текст события 1000."""

    def test_windows_user_and_exe(self, monkeypatch):
        """Проверить: собранный exe — путь от GetModuleFileName,
        пользователь ДОМЕН\\имя, запуск без окна.
        """
        win32api = types.ModuleType("win32api")
        win32api.GetModuleFileName = mock.Mock(
            return_value=r"C:\Program Files\FilePickerAPI\FilePickerAPI.exe"
        )
        win32api.GetUserNameEx = mock.Mock(return_value="BRANCH\\operator")
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        monkeypatch.setattr(runner, "_windows", lambda: True)
        monkeypatch.setattr(runner, "__compiled__", object(), raising=False)
        build_config = types.ModuleType("app._build_config")
        build_config.BRANCH_NAME = "5400"
        monkeypatch.setitem(sys.modules, "app._build_config", build_config)

        text = runner.startup_message(console=False)

        assert "филиал 5400" in text
        assert "FilePickerAPI.exe" in text
        assert "пользователь BRANCH\\operator" in text
        assert "запуск без окна" in text
        win32api.GetUserNameEx.assert_called_once_with(runner.NAME_SAM_COMPATIBLE)

    def test_user_fallbacks(self, monkeypatch):
        """Проверить: без GetUserNameEx — getpass, затем пометка."""
        win32api = types.ModuleType("win32api")
        win32api.GetUserNameEx = mock.Mock(side_effect=OSError("нет домена"))
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        monkeypatch.setattr(runner, "_windows", lambda: True)
        with mock.patch("getpass.getuser", return_value="operator"):
            assert runner._user() == "operator"  # noqa: SLF001
        with mock.patch("getpass.getuser", side_effect=OSError("нет")):
            assert runner._user() == "не определён"  # noqa: SLF001

    def test_branch(self, monkeypatch):
        """Проверить филиал без файла сборки и без BRANCH_NAME."""
        monkeypatch.setitem(sys.modules, "app._build_config", None)
        assert runner._branch() == "не задан (запуск из исходного кода)"  # noqa: SLF001
        monkeypatch.setitem(
            sys.modules, "app._build_config", types.ModuleType("app._build_config")
        )
        assert runner._branch() == "не задан"  # noqa: SLF001

    def test_branch_other_import_error(self, monkeypatch):
        error = ModuleNotFoundError("x", name="pydantic")
        monkeypatch.setattr(
            runner.importlib, "import_module", mock.Mock(side_effect=error)
        )
        with pytest.raises(ModuleNotFoundError):
            runner._branch()  # noqa: SLF001


class TestInstanceCheck:
    """Опрос порта и текст события второго экземпляра."""

    def test_port_in_use_by_connect(self, monkeypatch):
        monkeypatch.setattr(instance_check, "_windows", lambda: False)
        number = free_port()
        assert not instance_check.port_in_use("127.0.0.1", number)
        sock = blocker(number)
        try:
            assert instance_check.port_in_use("127.0.0.1", number)
        finally:
            sock.close()

    @pytest.mark.parametrize(
        ("owner", "expected"), [(1234, True), (None, False), (OSError("x"), "connect")]
    )
    def test_port_in_use_on_windows(self, monkeypatch, owner, expected):
        """Проверить: на Windows — таблица TCP, при отказе — connect."""
        from app import winapi

        monkeypatch.setattr(instance_check, "_windows", lambda: True)
        port_owner_pid = mock.Mock(
            side_effect=owner if isinstance(owner, Exception) else None,
            return_value=owner,
        )
        monkeypatch.setattr(winapi, "port_owner_pid", port_owner_pid)
        connects = mock.Mock(return_value=True)
        monkeypatch.setattr(instance_check, "_connects", connects)

        result = instance_check.port_in_use("127.0.0.1", 8000)

        port_owner_pid.assert_called_once_with(8000, "127.0.0.1")
        if expected == "connect":
            assert result
            connects.assert_called_once()
        else:
            assert result is expected
            connects.assert_not_called()

    @pytest.mark.parametrize(
        ("title", "fail", "expected"),
        [("File Picker API", 0, app.__version__), ("Other", 0, None), (None, 5, None)],
    )
    def test_probe_openapi(self, title, fail, expected):
        number = free_port()
        server = OpenApiServer(number, title=title, fail=fail)
        try:
            assert instance_check.probe_openapi("127.0.0.1", number) == expected
        finally:
            server.close()

    @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"info": []}'])
    def test_probe_openapi_bad_answer(self, monkeypatch, body):
        response = mock.Mock(status=200)
        response.read.return_value = body
        connection = mock.Mock()
        connection.getresponse.return_value = response
        monkeypatch.setattr(
            instance_check.http.client, "HTTPConnection", lambda *_a, **_k: connection
        )
        assert instance_check.probe_openapi("127.0.0.1", 8000) is None
        connection.close.assert_called_once()

    @pytest.mark.parametrize("connect_delay", [0, 0.4])
    def test_probe_openapi_slow_answer(self, monkeypatch, connect_delay):
        """Проверить: ответ по байту не держит запрос дольше срока,
        в том числе когда срок истёк ещё при соединении.
        """
        monkeypatch.setattr(instance_check, "REQUEST_TIMEOUT", 0.3)
        connect = socket.create_connection

        def slow_connect(*args, **kwargs):
            time.sleep(connect_delay)
            return connect(*args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", slow_connect)
        listener = socket.create_server(("127.0.0.1", 0))
        number = listener.getsockname()[1]
        stop = threading.Event()

        def trickle():
            client, _ = listener.accept()
            with client:
                for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"x" * 1000:
                    if stop.wait(0.05):
                        return
                    client.sendall(bytes([byte]))

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            assert instance_check.probe_openapi("127.0.0.1", number) is None
            assert time.monotonic() - started < 2
        finally:
            stop.set()
            thread.join(5)
            listener.close()

    def test_abort_closed_socket(self):
        """Проверить: прерывание уже закрытого сокета — тихо."""
        sock = mock.Mock()
        sock.shutdown.side_effect = OSError
        instance_check._abort(sock)  # noqa: SLF001 - помощник модуля

    def test_probe_openapi_nobody(self):
        assert instance_check.probe_openapi("127.0.0.1", free_port()) is None

    def test_wait_for_port_deadline(self, monkeypatch, clock):
        """Проверить: без ответа File Picker API за срок — OTHER."""
        monkeypatch.setattr(instance_check, "clock", clock)
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: True)

        def probe(*_):
            clock.advance(4)

        monkeypatch.setattr(instance_check, "probe_openapi", probe)
        result = instance_check.wait_for_port(threading.Event(), 10, "h", 1)
        assert result == ProbeResult(Probe.OTHER)

    def test_wait_for_port_answer_after_ten_seconds(self, monkeypatch, clock):
        """Проверить: ответ на запрос, начатый до конца срока, —
        FILE_PICKER.
        """
        monkeypatch.setattr(instance_check, "clock", clock)
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: True)
        answers = iter([None, None, "2.0.0"])

        def probe(*_):
            clock.advance(4.5)
            return next(answers)

        monkeypatch.setattr(instance_check, "probe_openapi", probe)
        result = instance_check.wait_for_port(threading.Event(), 10, "h", 1)
        assert result == ProbeResult(Probe.FILE_PICKER, "2.0.0")

    def test_wait_for_port_stop_during_request(self, monkeypatch, clock):
        """Проверить: остановка во время запроса — STOPPED, а не OTHER
        по истёкшему сроку.
        """
        monkeypatch.setattr(instance_check, "clock", clock)
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: True)
        stop = threading.Event()

        def probe(*_):
            clock.advance(20)
            stop.set()

        monkeypatch.setattr(instance_check, "probe_openapi", probe)
        assert instance_check.wait_for_port(stop, 10, "h", 1).status is Probe.STOPPED

    def test_wait_for_port_budget(self, monkeypatch, clock):
        """Проверить: запрос и пауза получают остаток срока, опрос на
        3 секунды заканчивается не позже 3 секунд.
        """
        monkeypatch.setattr(instance_check, "PROBE_INTERVAL", 0.5)
        monkeypatch.setattr(instance_check, "clock", clock)
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: True)
        timeouts = []

        def probe(_host, _port, timeout):
            timeouts.append(timeout)
            clock.advance(timeout)

        stop = mock.Mock()
        stop.is_set.return_value = False

        def wait(timeout):
            timeouts.append(timeout)
            clock.advance(timeout)
            return False

        stop.wait.side_effect = wait
        monkeypatch.setattr(instance_check, "probe_openapi", probe)
        started = clock.now
        result = instance_check.wait_for_port(stop, 3, "h", 1)
        assert result == ProbeResult(Probe.OTHER)
        assert clock.now - started <= 3
        assert timeouts == [2, 0.5, 0.5, 0]

    def test_wait_for_port_stopped(self, monkeypatch):
        stop = threading.Event()
        stop.set()
        assert instance_check.wait_for_port(stop, 10, "h", 1).status is Probe.STOPPED

        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        monkeypatch.setattr(instance_check, "port_in_use", lambda *_: True)
        monkeypatch.setattr(instance_check, "probe_openapi", lambda *_: None)
        assert instance_check.wait_for_port(stop, 10, "h", 1).status is Probe.STOPPED

    @pytest.mark.parametrize(
        ("owner", "pid_text"),
        [
            (4321, "PID 4321"),
            (None, "PID не определён"),
            (OSError(), "PID не определён"),
        ],
    )
    def test_message_on_windows(self, monkeypatch, owner, pid_text):
        from app import winapi

        monkeypatch.setattr(instance_check, "_windows", lambda: True)
        monkeypatch.setattr(
            winapi,
            "port_owner_pid",
            mock.Mock(
                side_effect=owner if isinstance(owner, Exception) else None,
                return_value=owner,
            ),
        )
        event_id, text = instance_check.second_instance_message(
            app.__version__, "127.0.0.1", 8000
        )
        assert event_id == 1005
        assert f"({pid_text}, версия {app.__version__})" in text

    def test_message_without_version(self, monkeypatch):
        monkeypatch.setattr(instance_check, "_windows", lambda: False)
        event_id, text = instance_check.second_instance_message("", "h", 1)
        assert event_id == 1006
        assert "(версия не указана)" in text


@pytest.fixture
def fake_window(monkeypatch):
    """Поддельные win32api и win32gui для консольного обработчика и
    скрытого окна; PumpMessages возвращается сразу.
    """
    win32api = types.ModuleType("win32api")
    win32api.SetConsoleCtrlHandler = mock.Mock()
    win32api.GetModuleHandle = mock.Mock(return_value=77)
    win32gui = types.ModuleType("win32gui")
    win32gui.WNDCLASS = types.SimpleNamespace
    win32gui.RegisterClass = mock.Mock()
    win32gui.CreateWindow = mock.Mock(return_value=99)
    win32gui.PumpMessages = mock.Mock()
    win32gui.DefWindowProc = mock.Mock(return_value=42)
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setattr(stop_signals, "_windows", lambda: True)
    monkeypatch.setattr(stop_signals, "_console_handler", None)
    return types.SimpleNamespace(win32api=win32api, win32gui=win32gui)


def handlers():
    return stop_signals.StopHandlers(mock.Mock(), mock.Mock())


class TestStopSignals:
    """Ctrl+C, закрытие консоли, скрытое окно и сигналы."""

    @pytest.mark.parametrize(
        ("ctrl_type", "kind", "reason"),
        [
            (0, "stop", "Ctrl+C в консоли"),
            (1, "stop", "Ctrl+Break в консоли"),
            (2, "urgent_stop", "закрытие консоли"),
            (5, None, None),
            (6, None, None),
        ],
    )
    def test_console_handler(self, ctrl_type, kind, reason):
        """Проверить: Ctrl+C и Ctrl+Break — обычная остановка, закрытие
        консоли — срочная, обе с True; выход и выключение
        (CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT) идут дальше по цепочке.
        """
        stops = handlers()
        assert stop_signals.console_handler(stops, ctrl_type) is (kind is not None)
        for name in ("stop", "urgent_stop"):
            called = getattr(stops, name)
            if name == kind:
                called.assert_called_once_with(reason)
            else:
                called.assert_not_called()

    @pytest.mark.parametrize(
        ("lparam", "reason"),
        [
            (0x1, "обновление или удаление программы"),
            (0x1 | 0x80000000, "обновление или удаление программы"),
            (0x80000000, "выход пользователя"),
            # LPARAM знаковый: старший бит может прийти отрицательным
            (-0x80000000, "выход пользователя"),
            (0x40000000, "выключение Windows"),
            (0, "выключение Windows"),
        ],
    )
    def test_end_session_reason(self, lparam, reason):
        assert stop_signals.end_session_reason(lparam) == reason

    def test_window_close(self):
        """Проверить: WM_CLOSE — обычная остановка."""
        stops = handlers()
        assert stop_signals.window_procedure(stops, 1, stop_signals.WM_CLOSE, 0, 0) == 0
        stops.stop.assert_called_once_with("сообщение WM_CLOSE")
        stops.urgent_stop.assert_not_called()

    def test_cancelled_logoff(self):
        """Проверить: WM_QUERYENDSESSION — только «можно», затем
        WM_ENDSESSION с wParam = FALSE (выход отменён) — ничего.
        """
        stops = handlers()
        procedure = functools.partial(stop_signals.window_procedure, stops, 1)
        assert procedure(stop_signals.WM_QUERYENDSESSION, 0, 0x80000000) == 1
        assert procedure(stop_signals.WM_ENDSESSION, 0, 0x80000000) == 0
        stops.stop.assert_not_called()
        stops.urgent_stop.assert_not_called()

    def test_end_session(self):
        """Проверить: WM_ENDSESSION с wParam = TRUE — срочная остановка
        с причиной по lParam.
        """
        stops = handlers()
        result = stop_signals.window_procedure(
            stops, 1, stop_signals.WM_ENDSESSION, 1, stop_signals.ENDSESSION_CLOSEAPP
        )
        assert result == 0
        stops.urgent_stop.assert_called_once_with("обновление или удаление программы")
        stops.stop.assert_not_called()

    def test_other_message(self, fake_window):
        """Проверить: прочие сообщения — DefWindowProc."""
        stops = handlers()
        assert stop_signals.window_procedure(stops, 1, 0x0001, 2, 3) == 42
        fake_window.win32gui.DefWindowProc.assert_called_once_with(1, 0x0001, 2, 3)

    def test_install_on_windows(self, fake_window):
        """Проверить: консольный обработчик и скрытое окно верхнего
        уровня класса FilePickerAPI.Instance с циклом сообщений.
        """
        win32api, win32gui = fake_window.win32api, fake_window.win32gui
        stop, urgent_stop = mock.Mock(), mock.Mock()

        INSTALL(stop, urgent_stop)

        [(handler, add)] = [c.args for c in win32api.SetConsoleCtrlHandler.mock_calls]
        assert add is True
        assert stop_signals._console_handler is handler  # noqa: SLF001
        assert handler(stop_signals.CTRL_C_EVENT) is True
        stop.assert_called_once_with("Ctrl+C в консоли")
        assert handler(stop_signals.CTRL_CLOSE_EVENT) is True
        urgent_stop.assert_called_once_with("закрытие консоли")

        [(window_class,)] = [c.args for c in win32gui.RegisterClass.mock_calls]
        assert window_class.lpszClassName == "FilePickerAPI.Instance"
        assert window_class.hInstance == 77
        [(class_name, _title, style, *rest)] = [
            c.args for c in win32gui.CreateWindow.mock_calls
        ]
        assert class_name == "FilePickerAPI.Instance"
        # Без WS_VISIBLE и без родителя: не HWND_MESSAGE
        assert style == 0
        assert rest[4] == 0
        stop.reset_mock()
        window_class.lpfnWndProc(99, stop_signals.WM_CLOSE, 0, 0)
        stop.assert_called_once_with("сообщение WM_CLOSE")
        wait_for(lambda: win32gui.PumpMessages.called)

    def test_window_fails(self, fake_window, caplog):
        """Проверить: окно не создалось — предупреждение, установка не
        ждёт срока и не падает.
        """
        fake_window.win32gui.RegisterClass.side_effect = OSError("класс занят")
        started = time.monotonic()
        INSTALL(mock.Mock(), mock.Mock())
        assert time.monotonic() - started < stop_signals.WINDOW_READY_SECONDS
        assert "Не удалось создать скрытое окно сервиса: класс занят" in caplog.text
        fake_window.win32gui.PumpMessages.assert_not_called()

    def test_install_on_windows_fails(self, fake_window, caplog):
        fake_window.win32api.SetConsoleCtrlHandler.side_effect = OSError("нет")
        INSTALL(mock.Mock(), mock.Mock())
        assert "Не удалось установить обработчик Ctrl+C: нет" in caplog.text
        assert stop_signals._console_handler is None  # noqa: SLF001

    @pytest.fixture
    def posix_handler(self, monkeypatch):
        """Обработчик SIGTERM вне Windows, без настоящих сигналов."""
        monkeypatch.setattr(stop_signals, "_windows", lambda: False)
        monkeypatch.setattr(stop_signals, "_pending", None)
        monkeypatch.setattr(stop_signals, "_stop", None)

        def install(stop):
            with mock.patch.object(stop_signals.signal, "signal") as set_signal:
                INSTALL(stop, mock.Mock())
            assert [c.args[0] for c in set_signal.call_args_list] == [
                signal.SIGINT,
                signal.SIGTERM,
            ]
            return set_signal.call_args.args[1]

        return install

    def test_install_signals(self, posix_handler):
        """Проверить: вне Windows SIGINT и SIGTERM — остановка с первой
        причиной, но только когда главный поток передаст её (deliver).
        """
        stop = mock.Mock()
        stop_signals.deliver()
        handler = posix_handler(stop)
        handler(signal.SIGTERM, None)
        handler(signal.SIGINT, None)
        stop.assert_not_called()
        stop_signals.deliver()
        stop_signals.deliver()
        stop.assert_called_once_with("сигнал SIGTERM")

    def test_signal_while_event_lock_held(self, posix_handler):
        """Проверить: сигнал, пришедший, пока главный поток держит замок
        service.decided (внутри decided.wait), не ждёт этот замок.

        Обработчик вызывается в потоке, который сам держит замок
        события, как главный поток при сигнале; раньше он ждал его
        вечно в request_stop → decided.set().
        """
        service = runner.Service(console=False)
        handler = posix_handler(service.request_stop)

        def signal_under_lock():
            with service.decided._cond:  # noqa: SLF001 - замок события
                handler(signal.SIGTERM, None)

        thread = threading.Thread(target=signal_under_lock, daemon=True)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        stop_signals.deliver()
        assert service.decided.is_set()
        assert service.stop_reason == "сигнал SIGTERM"

    @pytest.mark.usefixtures("port")
    def test_run_delivers_signal(self, service_env, posix_handler, monkeypatch):
        """Проверить: run(), ожидая исход, сам вызывает остановку по
        причине, которую запомнил обработчик сигнала.
        """
        handlers = []
        monkeypatch.setattr(
            stop_signals,
            "install",
            lambda stop, _urgent_stop: handlers.append(posix_handler(stop)),
        )

        def serve(server, _sockets=None):
            server.started = True
            server.service.on_started()
            handlers[0](signal.SIGTERM, None)
            wait_for(lambda: server.should_exit, 5)

        monkeypatch.setattr(runner.InstanceServer, "run", serve)
        assert runner.run() == 0
        assert service_env.exits == [0]

    def test_platform(self):
        assert stop_signals._windows() is (sys.platform == "win32")  # noqa: SLF001

    @windows_only
    def test_constants_match_pywin32(self):
        import win32con

        assert stop_signals.CTRL_C_EVENT == win32con.CTRL_C_EVENT
        assert stop_signals.CTRL_BREAK_EVENT == win32con.CTRL_BREAK_EVENT
        assert stop_signals.CTRL_CLOSE_EVENT == win32con.CTRL_CLOSE_EVENT
        assert stop_signals.WM_CLOSE == win32con.WM_CLOSE
        assert stop_signals.WM_QUERYENDSESSION == win32con.WM_QUERYENDSESSION
        assert stop_signals.WM_ENDSESSION == win32con.WM_ENDSESSION
        # В win32con константа знаковая, ENDSESSION_CLOSEAPP в нём нет
        assert stop_signals.ENDSESSION_LOGOFF == win32con.ENDSESSION_LOGOFF & 0xFFFFFFFF


def wait_for_service(process, timeout=30):
    """Дождаться ответа сервиса на порту 8000."""
    answer = {}

    def ready():
        if process.poll() is not None:
            pytest.fail(f"процесс завершился с кодом {process.returncode}")
        try:
            answer.update(
                httpx.get("http://127.0.0.1:8000/openapi.json", timeout=1).json()
            )
        except httpx.HTTPError:
            return False
        return True

    wait_for(ready, timeout)
    return answer


def instance_window(pid, timeout=10):
    """Скрытое окно FilePickerAPI.Instance процесса pid."""
    import win32gui
    import win32process

    found = []

    def collect(hwnd, _):
        if (
            win32gui.GetClassName(hwnd) == stop_signals.WINDOW_CLASS
            and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
        ):
            found.append(hwnd)
        return True

    def search():
        win32gui.EnumWindows(collect, None)
        return bool(found)

    wait_for(search, timeout)
    return found[0]


def service_code(log_file):
    """Код для `python -c`: сервис без журнала событий, файл — во
    временной папке, а перед выходом — загруженные модули pywin32.
    """
    return (
        "import os, pathlib, sys\n"
        "import app.__main__, app.runner\n"
        "import app.logging_setup as logging_setup\n"
        "logging_setup.event_source_registered = lambda: False\n"
        f"logging_setup.log_file_path = lambda: pathlib.Path({str(log_file)!r})\n"
        "PYWIN32 = {'win32api', 'win32con', 'win32gui', 'win32evtlog',\n"
        "           'win32security', 'win32com', 'pywintypes'}\n"
        "def _exit(code):\n"
        "    print('PYWIN32', sorted(PYWIN32 & set(sys.modules)), flush=True)\n"
        "    os._exit(code)\n"
        "app.runner._exit = _exit\n"
        "sys.exit(app.__main__.main([]))\n"
    )


def start_service(tmp_path, **kwargs):
    return subprocess.Popen(  # noqa: S603 - аргументы задаются тестом
        [sys.executable, "-c", service_code(tmp_path / "logs" / "FilePickerAPI.log")],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        **kwargs,
    )


@pytest.fixture
def service_process(tmp_path):
    """Отдельный процесс сервиса на настоящем порте 8000."""
    processes = []

    def start(**kwargs):
        process = start_service(tmp_path, **kwargs)
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate()


@linux_only
class TestLinuxProcess:
    """`python -m app` на Linux без pywin32 и подмен (задача 4.10)."""

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_stops_by_signal(self, service_process, signum):
        process = service_process()
        info = wait_for_service(process)
        assert info["info"]["title"] == "File Picker API"
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert f"Сервис остановлен: сигнал {signal.Signals(signum).name}" in stderr
        assert "PYWIN32 []" in stdout

    def test_second_launch(self, service_process):
        first = service_process()
        wait_for_service(first)
        second = service_process()
        stdout, stderr = second.communicate(timeout=30)
        assert second.returncode == 0, stderr
        assert f"Сервис уже работает в другом процессе (версия {app.__version__})" in (
            stderr
        )
        assert "PYWIN32 []" in stdout
        first.send_signal(signal.SIGTERM)
        first.communicate(timeout=30)

    def test_settings_error(self, tmp_path):
        (tmp_path / ".env").write_text("FILES_DIRECTORY=/x\n", encoding="utf-8")
        result = run_python("-c", service_code(tmp_path / "log"), cwd=tmp_path)
        assert result.returncode == 1
        assert "Ошибка настроек, сервис не запущен" in result.stderr
        assert "PYWIN32 []" in result.stdout


@windows_only
class TestWindowsProcess:
    """Остановка с настоящим pywin32 отдельным процессом."""

    def test_ctrl_break(self, service_process, tmp_path):
        """Проверить: CTRL_BREAK_EVENT — код 0 и 1001 в журнале."""
        # Сигнал доходит только через общую консоль (CI-агент без
        # консоли, служба, планировщик)
        if not ctypes.windll.kernel32.GetConsoleCP():
            pytest.skip("у процесса pytest нет консоли")
        process = service_process(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        wait_for_service(process)
        process.send_signal(signal.CTRL_BREAK_EVENT)
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        log_text = (tmp_path / "logs" / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert "INFO app.runner: Сервис остановлен: Ctrl+Break в консоли" in log_text

    def test_wm_close(self, service_process, tmp_path):
        """Проверить: WM_CLOSE окну, найденному по классу, — код 0 и
        1001 в журнале.
        """
        import win32con
        import win32gui

        process = service_process()
        wait_for_service(process)
        win32gui.PostMessage(instance_window(process.pid), win32con.WM_CLOSE, 0, 0)
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        log_text = (tmp_path / "logs" / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert "INFO app.runner: Сервис остановлен: сообщение WM_CLOSE" in log_text

    def test_end_session(self, service_process, tmp_path):
        """Проверить: отменённый выход — сервис работает; WM_ENDSESSION
        от Restart Manager — 1001 до возврата из обработчика, код 0.
        """
        import pywintypes
        import win32con
        import win32gui

        process = service_process()
        wait_for_service(process)
        hwnd = instance_window(process.pid)
        assert win32gui.SendMessage(hwnd, win32con.WM_QUERYENDSESSION, 0, 0) == 1
        win32gui.SendMessage(hwnd, win32con.WM_ENDSESSION, 0, 0)
        assert wait_for_service(process)["info"]["title"] == "File Picker API"
        # Обработчик не возвращается: процесс завершается раньше
        with contextlib.suppress(pywintypes.error):
            win32gui.SendMessageTimeout(
                hwnd,
                win32con.WM_ENDSESSION,
                1,
                stop_signals.ENDSESSION_CLOSEAPP,
                win32con.SMTO_ABORTIFHUNG,
                30_000,
            )
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        log_text = (tmp_path / "logs" / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert (
            "INFO app.runner: Сервис остановлен: обновление или удаление программы"
            in log_text
        )
        assert log_text.count("Сервис остановлен") == 1
