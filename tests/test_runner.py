"""Тесты экземпляра сервиса: запуск, второй экземпляр, остановка.

run() выполняется в процессе теста: выход процесса (_exit) и
обработчики сигналов подменены (фикстура service_env), журнал событий —
поддельный win32evtlog. Uvicorn в части тестов настоящий, на свободном
порте вместо 8000.
"""

import ctypes
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
from tests.conftest import start_then_stop, wait_for
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
    return [call.args[3] for call in evtlog.ReportEvent.call_args_list]


def event_texts(evtlog, event_id):
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


class TestStopSignals:
    """Обработчики Ctrl+C и сигналов."""

    @pytest.mark.parametrize(
        ("ctrl_type", "reason"),
        [(0, "Ctrl+C в консоли"), (1, "Ctrl+Break в консоли"), (2, None), (5, None)],
    )
    def test_console_handler(self, ctrl_type, reason):
        """Проверить: Ctrl+C и Ctrl+Break — остановка и True, прочие
        события консоли идут дальше по цепочке.
        """
        stop = mock.Mock()
        assert stop_signals.console_handler(stop, ctrl_type) is (reason is not None)
        if reason is None:
            stop.assert_not_called()
        else:
            stop.assert_called_once_with(reason)

    def test_install_on_windows(self, monkeypatch):
        win32api = types.ModuleType("win32api")
        win32api.SetConsoleCtrlHandler = mock.Mock()
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        monkeypatch.setattr(stop_signals, "_windows", lambda: True)
        monkeypatch.setattr(stop_signals, "_console_handler", None)
        stop = mock.Mock()

        INSTALL(stop)

        [(handler, add)] = [c.args for c in win32api.SetConsoleCtrlHandler.mock_calls]
        assert add is True
        assert stop_signals._console_handler is handler  # noqa: SLF001
        assert handler(stop_signals.CTRL_C_EVENT) is True
        stop.assert_called_once_with("Ctrl+C в консоли")

    def test_install_on_windows_fails(self, monkeypatch, caplog):
        win32api = types.ModuleType("win32api")
        win32api.SetConsoleCtrlHandler = mock.Mock(side_effect=OSError("нет"))
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        monkeypatch.setattr(stop_signals, "_windows", lambda: True)
        monkeypatch.setattr(stop_signals, "_console_handler", None)
        INSTALL(mock.Mock())
        assert "Не удалось установить обработчик Ctrl+C: нет" in caplog.text
        assert stop_signals._console_handler is None  # noqa: SLF001

    def test_install_signals(self, monkeypatch):
        """Проверить: вне Windows SIGINT и SIGTERM — остановка."""
        monkeypatch.setattr(stop_signals, "_windows", lambda: False)
        with mock.patch.object(stop_signals.signal, "signal") as set_signal:
            stop = mock.Mock()
            INSTALL(stop)
        assert [c.args[0] for c in set_signal.call_args_list] == [
            signal.SIGINT,
            signal.SIGTERM,
        ]
        handler = set_signal.call_args.args[1]
        handler(signal.SIGTERM, None)
        stop.assert_called_once_with("сигнал SIGTERM")

    def test_platform(self):
        assert stop_signals._windows() is (sys.platform == "win32")  # noqa: SLF001

    @windows_only
    def test_constants_match_pywin32(self):
        import win32con

        assert stop_signals.CTRL_C_EVENT == win32con.CTRL_C_EVENT
        assert stop_signals.CTRL_BREAK_EVENT == win32con.CTRL_BREAK_EVENT


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
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
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
