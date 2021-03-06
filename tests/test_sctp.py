import asyncio
import logging
from unittest import TestCase

from aiortc import sctp

from .utils import dummy_transport_pair, load, run


class SctpPacketTest(TestCase):
    def test_parse_init(self):
        data = load('sctp_init.bin')
        packet = sctp.Packet.parse(data)
        self.assertEqual(packet.source_port, 5000)
        self.assertEqual(packet.destination_port, 5000)
        self.assertEqual(packet.verification_tag, 0)

        self.assertEqual(len(packet.chunks), 1)
        self.assertTrue(isinstance(packet.chunks[0], sctp.InitChunk))
        self.assertEqual(packet.chunks[0].type, 1)
        self.assertEqual(packet.chunks[0].flags, 0)
        self.assertEqual(len(packet.chunks[0].body), 82)

        self.assertEqual(bytes(packet), data)

    def test_parse_cookie_echo(self):
        data = load('sctp_cookie_echo.bin')
        packet = sctp.Packet.parse(data)
        self.assertEqual(packet.source_port, 5000)
        self.assertEqual(packet.destination_port, 5000)
        self.assertEqual(packet.verification_tag, 1039286925)

        self.assertEqual(len(packet.chunks), 1)
        self.assertTrue(isinstance(packet.chunks[0], sctp.CookieEchoChunk))
        self.assertEqual(packet.chunks[0].type, 10)
        self.assertEqual(packet.chunks[0].flags, 0)
        self.assertEqual(len(packet.chunks[0].body), 8)

        self.assertEqual(bytes(packet), data)

    def test_parse_abort(self):
        data = load('sctp_abort.bin')
        packet = sctp.Packet.parse(data)
        self.assertEqual(packet.source_port, 5000)
        self.assertEqual(packet.destination_port, 5000)
        self.assertEqual(packet.verification_tag, 3763951554)

        self.assertEqual(len(packet.chunks), 1)
        self.assertTrue(isinstance(packet.chunks[0], sctp.AbortChunk))
        self.assertEqual(packet.chunks[0].type, 6)
        self.assertEqual(packet.chunks[0].flags, 0)
        self.assertEqual(packet.chunks[0].params, [
            (13, b'Expected B-bit for TSN=4ce1f17f, SID=0001, SSN=0000'),
        ])

        self.assertEqual(bytes(packet), data)

    def test_invalid_checksum(self):
        data = load('sctp_init.bin')
        data = data[0:8] + b'\x01\x02\x03\x04' + data[12:]
        with self.assertRaises(ValueError) as cm:
            sctp.Packet.parse(data)
        self.assertEqual(str(cm.exception), 'SCTP packet has invalid checksum')

    def test_truncated_packet_header(self):
        data = load('sctp_init.bin')[0:10]
        with self.assertRaises(ValueError) as cm:
            sctp.Packet.parse(data)
        self.assertEqual(str(cm.exception), 'SCTP packet length is less than 12 bytes')


class SctpAssociationTest(TestCase):
    def test_ok(self):
        client_transport, server_transport = dummy_transport_pair()
        client = sctp.Endpoint(is_server=False, transport=client_transport)
        server = sctp.Endpoint(is_server=True, transport=server_transport)
        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client.run())

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(client.state, sctp.Endpoint.State.ESTABLISHED)
        self.assertEqual(server.state, sctp.Endpoint.State.ESTABLISHED)

        # DATA_CHANNEL_OPEN
        run(client.send(1, 50, b'\x03\x00\x00\x00\x00\x00\x00\x00\x00\x04\x00\x00chat'))
        stream_id, protocol, data = run(server.recv())
        self.assertEqual(stream_id, 1)
        self.assertEqual(protocol, 50)
        self.assertEqual(data, b'\x03\x00\x00\x00\x00\x00\x00\x00\x00\x04\x00\x00chat')

        # shutdown
        run(client.close())
        run(server.close())
        self.assertEqual(client.state, sctp.Endpoint.State.CLOSED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

    def test_abort(self):
        client_transport, server_transport = dummy_transport_pair()
        client = sctp.Endpoint(is_server=False, transport=client_transport)
        server = sctp.Endpoint(is_server=True, transport=server_transport)
        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client.run())

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(client.state, sctp.Endpoint.State.ESTABLISHED)
        self.assertEqual(server.state, sctp.Endpoint.State.ESTABLISHED)

        # shutdown
        run(client.abort())
        run(asyncio.sleep(0.5))
        self.assertEqual(client.state, sctp.Endpoint.State.CLOSED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

    def test_garbage(self):
        client_transport, server_transport = dummy_transport_pair()
        server = sctp.Endpoint(is_server=True, transport=server_transport)
        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client_transport.send(b'garbage'))

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

        # shutdown
        run(server.close())

    def test_bad_verification_tag(self):
        # verification tag is 12345 instead of 0
        data = load('sctp_init_bad_verification.bin')

        client_transport, server_transport = dummy_transport_pair()
        server = sctp.Endpoint(is_server=True, transport=server_transport)
        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client_transport.send(data))

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

        # shutdown
        run(server.close())

    def test_bad_cookie(self):
        client_transport, server_transport = dummy_transport_pair()
        client = sctp.Endpoint(is_server=False, transport=client_transport)
        server = sctp.Endpoint(is_server=True, transport=server_transport)

        # corrupt cookie
        real_send_chunk = client._send_chunk

        async def mock_send_chunk(chunk):
            if isinstance(chunk, sctp.CookieEchoChunk):
                chunk.body = b'garbage'
            return await real_send_chunk(chunk)

        client._send_chunk = mock_send_chunk

        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client.run())

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(client.state, sctp.Endpoint.State.COOKIE_ECHOED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

        # shutdown
        run(client.close())
        run(server.close())
        self.assertEqual(client.state, sctp.Endpoint.State.CLOSED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

    def test_stale_cookie(self):
        def mock_timestamp():
            mock_timestamp.calls += 1
            if mock_timestamp.calls == 1:
                return 0
            else:
                return 61

        mock_timestamp.calls = 0

        client_transport, server_transport = dummy_transport_pair()
        client = sctp.Endpoint(is_server=False, transport=client_transport)
        server = sctp.Endpoint(is_server=True, transport=server_transport)
        server._get_timestamp = mock_timestamp
        asyncio.ensure_future(server.run())
        asyncio.ensure_future(client.run())

        # check outcome
        run(asyncio.sleep(0.5))
        self.assertEqual(client.state, sctp.Endpoint.State.CLOSED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)

        # shutdown
        run(client.close())
        run(server.close())
        self.assertEqual(client.state, sctp.Endpoint.State.CLOSED)
        self.assertEqual(server.state, sctp.Endpoint.State.CLOSED)


logging.basicConfig(level=logging.DEBUG)
