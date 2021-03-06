import ipaddress
import re

import aioice

from . import rtp

DIRECTIONS = [
    'sendrecv',
    'sendonly',
    'recvonly',
    'inactive'
]


def ipaddress_from_sdp(sdp):
    m = re.match('^IN (IP4|IP6) ([^ ]+)$', sdp)
    assert m
    return m.group(2)


def ipaddress_to_sdp(addr):
    version = ipaddress.ip_address(addr).version
    return 'IN IP%d %s' % (version, addr)


class MediaDescription:
    def __init__(self, kind, port, profile, fmt):
        # rtp
        self.kind = kind
        self.port = port
        self.host = None
        self.profile = profile
        self.direction = None

        # rtcp
        self.rtcp_port = None
        self.rtcp_host = None
        self.rtcp_mux = False

        # formats
        self.fmt = fmt
        self.rtpmap = {}
        self.sctpmap = {}

        # DTLS
        self.dtls_fingerprint = None
        self.dtls_setup = None

        # ICE
        self.ice_candidates = []
        self.ice_ufrag = None
        self.ice_pwd = None

    def __str__(self):
        lines = []
        lines.append('m=%s %d %s %s' % (
            self.kind,
            self.port,
            self.profile,
            ' '.join(map(str, self.fmt))
        ))
        lines.append('c=%s' % ipaddress_to_sdp(self.host))
        if self.direction is not None:
            lines.append('a=' + self.direction)

        if self.rtcp_port is not None and self.rtcp_host is not None:
            lines.append('a=rtcp:%d %s' % (self.rtcp_port, ipaddress_to_sdp(self.rtcp_host)))
        if self.rtcp_mux:
            lines.append('a=rtcp-mux')

        # ice
        for candidate in self.ice_candidates:
            lines.append('a=candidate:' + candidate.to_sdp())
        if self.ice_ufrag is not None:
            lines.append('a=ice-ufrag:' + self.ice_ufrag)
        if self.ice_pwd is not None:
            lines.append('a=ice-pwd:' + self.ice_pwd)

        # dtls
        if self.dtls_fingerprint:
            lines.append('a=fingerprint:sha-256 ' + self.dtls_fingerprint)
        if self.dtls_setup:
            lines.append('a=setup:' + self.dtls_setup)

        return '\r\n'.join(lines) + '\r\n'


class SessionDescription:
    def __init__(self):
        self.media = []

    @classmethod
    def parse(cls, sdp):
        current_media = None
        dtls_fingerprint = None
        session = cls()

        for line in sdp.splitlines():
            if line.startswith('m='):
                m = re.match('^m=([^ ]+) ([0-9]+) ([A-Z/]+) (.+)$', line)
                assert m

                # check payload types are valid
                kind = m.group(1)
                fmt = [int(x) for x in m.group(4).split()]
                if kind in ['audio', 'video']:
                    for pt in fmt:
                        assert pt >= 0 and pt < 256
                        assert pt not in rtp.FORBIDDEN_PAYLOAD_TYPES

                current_media = MediaDescription(
                    kind=kind,
                    port=int(m.group(2)),
                    profile=m.group(3),
                    fmt=fmt)
                current_media.dtls_fingerprint = dtls_fingerprint
                session.media.append(current_media)
            elif line.startswith('c=') and current_media:
                current_media.host = ipaddress_from_sdp(line[2:])
            elif line.startswith('a='):
                if ':' in line:
                    attr, value = line[2:].split(':', 1)
                else:
                    attr = line[2:]
                if current_media:
                    if attr == 'candidate':
                        current_media.ice_candidates.append(aioice.Candidate.from_sdp(value))
                    elif attr == 'fingerprint':
                        algo, fingerprint = value.split()
                        assert algo == 'sha-256'
                        current_media.dtls_fingerprint = fingerprint
                    elif attr == 'ice-ufrag':
                        current_media.ice_ufrag = value
                    elif attr == 'ice-pwd':
                        current_media.ice_pwd = value
                    elif attr == 'rtcp':
                        port, rest = value.split(' ', 1)
                        current_media.rtcp_port = int(port)
                        current_media.rtcp_host = ipaddress_from_sdp(rest)
                    elif attr == 'rtcp-mux':
                        current_media.rtcp_mux = True
                    elif attr == 'setup':
                        current_media.dtls_setup = value
                    elif attr in DIRECTIONS:
                        current_media.direction = attr
                    elif attr in ['rtpmap', 'sctpmap']:
                        format_id, format_desc = value.split(' ', 1)
                        getattr(current_media, attr)[int(format_id)] = format_desc
                else:
                    # session-level attributes
                    if attr == 'fingerprint':
                        algo, fingerprint = value.split()
                        assert algo == 'sha-256'
                        dtls_fingerprint = fingerprint

        return session
