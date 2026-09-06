import http.server
import socket
import socketserver
from http.server import BaseHTTPRequestHandler

import pytest

from caty_gateway.caty_gateway import (
    _GatewayHTTPServer,
    _NoReverseDNSBindMixin,
    _QRDeliveryHTTPServer,
    _bind_qr_delivery_server,
)


def _getfqdn_must_not_be_called(*_args, **_kwargs):
    raise AssertionError("getfqdn must not be called")


def _fake_bind(self):
    self.server_address = ("127.0.0.1", 8123)


def test_server_bind_does_not_call_getfqdn_without_socket(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    monkeypatch.setattr(socketserver.TCPServer, "server_bind", _fake_bind)
    server = _GatewayHTTPServer(
        ("127.0.0.1", 8123), BaseHTTPRequestHandler, bind_and_activate=False
    )
    try:
        server.server_bind()
        assert server.server_name == "127.0.0.1"
        assert server.server_port == 8123
    finally:
        server.server_close()


def test_server_bind_real_loopback_socket_skips_getfqdn(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    server = _GatewayHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert server.server_port == server.socket.getsockname()[1]
        assert server.server_name == "127.0.0.1"
    finally:
        server.server_close()


def test_stock_threading_http_server_calls_getfqdn(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    monkeypatch.setattr(socketserver.TCPServer, "server_bind", _fake_bind)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 8123), BaseHTTPRequestHandler, bind_and_activate=False
    )
    try:
        with pytest.raises(AssertionError, match="getfqdn must not be called"):
            server.server_bind()
    finally:
        server.server_close()


def test_qr_delivery_server_bind_does_not_call_getfqdn_without_socket(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    monkeypatch.setattr(socketserver.TCPServer, "server_bind", _fake_bind)
    server = _QRDeliveryHTTPServer(
        ("127.0.0.1", 8123), BaseHTTPRequestHandler, bind_and_activate=False
    )
    try:
        server.server_bind()
        assert server.server_name == "127.0.0.1"
        assert server.server_port == 8123
    finally:
        server.server_close()


def test_qr_delivery_server_is_single_threaded():
    assert not issubclass(_QRDeliveryHTTPServer, socketserver.ThreadingMixIn)
    assert issubclass(_QRDeliveryHTTPServer, http.server.HTTPServer)


def test_bind_qr_delivery_server_constructs_no_getfqdn_class(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    server = _bind_qr_delivery_server(BaseHTTPRequestHandler, ["127.0.0.1"])
    try:
        assert isinstance(server, _QRDeliveryHTTPServer)
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.socket.getsockname()[1]
    finally:
        server.server_close()


def test_stock_http_server_calls_getfqdn(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", _getfqdn_must_not_be_called)
    monkeypatch.setattr(socketserver.TCPServer, "server_bind", _fake_bind)
    server = http.server.HTTPServer(
        ("127.0.0.1", 8123), BaseHTTPRequestHandler, bind_and_activate=False
    )
    try:
        with pytest.raises(AssertionError, match="getfqdn must not be called"):
            server.server_bind()
    finally:
        server.server_close()


def test_both_servers_share_the_mixin_bind():
    assert _GatewayHTTPServer.server_bind is _NoReverseDNSBindMixin.server_bind
    assert _QRDeliveryHTTPServer.server_bind is _NoReverseDNSBindMixin.server_bind
