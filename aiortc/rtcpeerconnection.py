import asyncio
import datetime
import uuid

import aioice
from pyee import EventEmitter

from . import dtls, rtp, sctp, sdp
from .exceptions import InternalError, InvalidAccessError, InvalidStateError
from .rtcdatachannel import DataChannelManager
from .rtcrtptransceiver import (RemoteStreamTrack, RTCRtpReceiver,
                                RTCRtpSender, RTCRtpTransceiver)
from .rtcsctptransport import RTCSctpTransport
from .rtcsessiondescription import RTCSessionDescription

DUMMY_CANDIDATE = aioice.Candidate(
    foundation='',
    component=1,
    transport='udp',
    priority=1,
    host='0.0.0.0',
    port=0,
    type='host')
MEDIA_CODECS = [
    rtp.Codec(kind='audio', name='opus', clockrate=48000, channels=2),
    rtp.Codec(kind='audio', name='PCMU', clockrate=8000, channels=1, pt=0),
    rtp.Codec(kind='audio', name='PCMA', clockrate=8000, channels=1, pt=8),
    rtp.Codec(kind='video', name='VP8', clockrate=90000),
]
MEDIA_KINDS = ['audio', 'video']


def find_common_codecs(local_codecs, remote_media):
    common = []
    for pt in remote_media.fmt:
        bits = remote_media.rtpmap[pt].split('/')
        name = bits[0]
        clockrate = int(bits[1])

        for codec in local_codecs:
            if (codec.kind == remote_media.kind and
               codec.name == name and
               codec.clockrate == clockrate):
                if pt in rtp.DYNAMIC_PAYLOAD_TYPES:
                    codec = codec.clone(pt=pt)
                common.append(codec)
                break
    return common


def get_ntp_seconds():
    return int((
        datetime.datetime.utcnow() - datetime.datetime(1900, 1, 1, 0, 0, 0)
    ).total_seconds())


def transport_sdp(iceConnection, dtlsSession):
    sdp = []
    for candidate in iceConnection.local_candidates:
        sdp += ['a=candidate:%s' % candidate.to_sdp()]
    sdp += [
        'a=ice-pwd:%s' % iceConnection.local_password,
        'a=ice-ufrag:%s' % iceConnection.local_username,
    ]

    sdp += ['a=fingerprint:sha-256 %s' % dtlsSession.local_fingerprint]
    if dtlsSession.is_server:
        sdp += ['a=setup:actpass']
    else:
        sdp += ['a=setup:active']

    return sdp


class RTCPeerConnection(EventEmitter):
    """
    The RTCPeerConnection interface represents a WebRTC connection between
    the local computer and a remote peer.
    """
    def __init__(self, loop=None):
        super().__init__(loop=loop)
        self.__cname = '{%s}' % uuid.uuid4()
        self.__datachannelManager = None
        self.__dtlsContext = dtls.DtlsSrtpContext()
        self.__sctp = None
        self.__transceivers = []

        self.__iceConnectionState = 'new'
        self.__iceGatheringState = 'new'
        self.__isClosed = False
        self.__signalingState = 'stable'

        self.__currentLocalDescription = None
        self.__currentRemoteDescription = None

    @property
    def iceConnectionState(self):
        return self.__iceConnectionState

    @property
    def iceGatheringState(self):
        return self.__iceGatheringState

    @property
    def localDescription(self):
        """
        An :class:`RTCSessionDescription` describing the session for
        the local end of the connection.
        """
        return self.__currentLocalDescription

    @property
    def remoteDescription(self):
        """
        An :class:`RTCSessionDescription` describing the session for
        the remote end of the connection.
        """
        return self.__currentRemoteDescription

    @property
    def signalingState(self):
        return self.__signalingState

    def addTrack(self, track):
        """
        Add a new media track to the set of media tracks while will be
        transmitted to the other peer.
        """
        # check state is valid
        self.__assertNotClosed()
        if track.kind not in ['audio', 'video']:
            raise InternalError('Invalid track kind "%s"' % track.kind)

        # don't add track twice
        for sender in self.getSenders():
            if sender.track == track:
                raise InvalidAccessError('Track already has a sender')

        for transceiver in self.__transceivers:
            if transceiver._kind == track.kind:
                if transceiver.sender.track is None:
                    transceiver.sender._track = track
                    return transceiver.sender
                else:
                    raise InternalError('Only a single %s track is supported for now' % track.kind)

        transceiver = self.__createTransceiver(
            kind=track.kind,
            controlling=True,
            sender_track=track)
        return transceiver.sender

    async def close(self):
        """
        Terminate the ICE agent, ending ICE processing and streams.
        """
        if self.__isClosed:
            return
        self.__isClosed = True
        self.__setSignalingState('closed')
        for transceiver in self.__transceivers:
            await transceiver.stop()
            await transceiver._dtlsSession.close()
            await transceiver._iceConnection.close()
        if self.__sctp:
            await self.__sctpEndpoint.close()
            await self.__sctp._dtlsSession.close()
            await self.__sctp._iceConnection.close()
        self.__setIceConnectionState('closed')

    async def createAnswer(self):
        """
        Create an SDP answer to an offer received from a remote peer during
        the offer/answer negotiation of a WebRTC connection.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()
        if self.signalingState not in ['have-remote-offer', 'have-local-pranswer']:
            raise InvalidStateError('Cannot create answer in signaling state "%s"' %
                                    self.signalingState)

        return RTCSessionDescription(
            sdp=self.__createSdp(),
            type='answer')

    def createDataChannel(self, label, protocol=''):
        """
        Create a data channel with the given label.

        :rtype: :class:`RTCDataChannel`
        """
        if not self.__sctp:
            self.__createSctp(controlling=True)

        return self.__datachannelManager.create_channel(label=label, protocol=protocol)

    async def createOffer(self):
        """
        Create an SDP offer for the purpose of starting a new WebRTC
        connection to a remote peer.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()

        if not self.__sctp and not self.__transceivers:
            raise InternalError('Cannot create an offer with no media and no data channels')

        # offer codecs
        dynamic_pt = rtp.DYNAMIC_PAYLOAD_TYPES.start
        for transceiver in self.__transceivers:
            codecs = []
            for codec in MEDIA_CODECS:
                if codec.kind == transceiver._kind:
                    if codec.pt is None:
                        codecs.append(codec.clone(pt=dynamic_pt))
                        dynamic_pt += 1
                    else:
                        codecs.append(codec)
            transceiver._codecs = codecs

        return RTCSessionDescription(
            sdp=self.__createSdp(),
            type='offer')

    def getReceivers(self):
        return list(map(lambda x: x.receiver, self.__transceivers))

    def getSenders(self):
        return list(map(lambda x: x.sender, self.__transceivers))

    async def setLocalDescription(self, sessionDescription):
        """
        Change the local description associated with the connection.

        :param: sessionDescription: An :class:`RTCSessionDescription` generated
                                    by :meth:`createOffer` or :meth:`createAnswer()`.
        """
        if sessionDescription.type == 'offer':
            self.__setSignalingState('have-local-offer')
        elif sessionDescription.type == 'answer':
            self.__setSignalingState('stable')

        # gather
        await self.__gather()

        # connect
        asyncio.ensure_future(self.__connect())

        self.__currentLocalDescription = RTCSessionDescription(
            sdp=self.__createSdp(),
            type=sessionDescription.type)

    async def setRemoteDescription(self, sessionDescription):
        """
        Changes the remote description associated with the connection.

        :param: sessionDescription: An :class:`RTCSessionDescription` created from
                                    information received over the signaling channel.
        """
        # check description is compatible with signaling state
        if sessionDescription.type == 'offer':
            if self.signalingState not in ['stable', 'have-remote-offer']:
                raise InvalidStateError('Cannot handle offer in signaling state "%s"' %
                                        self.signalingState)
        elif sessionDescription.type == 'answer':
            if self.signalingState not in ['have-local-offer', 'have-remote-pranswer']:
                raise InvalidStateError('Cannot handle answer in signaling state "%s"' %
                                        self.signalingState)

        # parse description
        parsedRemoteDescription = sdp.SessionDescription.parse(sessionDescription.sdp)

        # apply description
        for media in parsedRemoteDescription.media:
            if media.kind in ['audio', 'video']:
                # find transceiver
                transceiver = None
                for t in self.__transceivers:
                    if t._kind == media.kind:
                        transceiver = t
                if transceiver is None:
                    transceiver = self.__createTransceiver(
                        kind=media.kind,
                        controlling=False)

                # negotiate codecs
                common = find_common_codecs(MEDIA_CODECS, media)
                assert len(common)
                transceiver._codecs = common

                # configure transport
                transceiver._iceConnection.remote_candidates = media.ice_candidates
                transceiver._iceConnection.remote_username = media.ice_ufrag
                transceiver._iceConnection.remote_password = media.ice_pwd
                transceiver._dtlsSession.remote_fingerprint = media.dtls_fingerprint

                if not transceiver.receiver._track:
                    transceiver.receiver._track = RemoteStreamTrack(kind=media.kind)
                    self.emit('track', transceiver.receiver._track)

            elif media.kind == 'application':
                if not self.__sctp:
                    self.__createSctp(controlling=False)

                # configure transport
                self.__sctp._iceConnection.remote_candidates = media.ice_candidates
                self.__sctp._iceConnection.remote_username = media.ice_ufrag
                self.__sctp._iceConnection.remote_password = media.ice_pwd
                self.__sctp._dtlsSession.remote_fingerprint = media.dtls_fingerprint

        # connect
        asyncio.ensure_future(self.__connect())

        # update signaling state
        if sessionDescription.type == 'offer':
            self.__setSignalingState('have-remote-offer')
        elif sessionDescription.type == 'answer':
            self.__setSignalingState('stable')

        self.__currentRemoteDescription = sessionDescription

    async def __connect(self):
        for iceConnection, dtlsSession in self.__transports():
            if (not iceConnection.local_candidates or not iceConnection.remote_candidates):
                return

        if self.iceConnectionState == 'new':
            self.__setIceConnectionState('checking')
            for iceConnection, dtlsSession in self.__transports():
                await iceConnection.connect()
                await dtlsSession.connect()
            for transceiver in self.__transceivers:
                asyncio.ensure_future(transceiver._run(transceiver._dtlsSession.rtp))
            if self.__sctp:
                asyncio.ensure_future(self.__sctpEndpoint.run())
                asyncio.ensure_future(self.__datachannelManager.run(self.__sctpEndpoint))
            self.__setIceConnectionState('completed')

    async def __gather(self):
        if self.__iceGatheringState == 'new':
            self.__setIceGatheringState('gathering')
            for iceConnection, dtlsSession in self.__transports():
                await iceConnection.gather_candidates()
            self.__setIceGatheringState('complete')

    def __assertNotClosed(self):
        if self.__isClosed:
            raise InvalidStateError('RTCPeerConnection is closed')

    def __createSctp(self, controlling):
        self.__sctp = RTCSctpTransport()
        self.__createTransport(self.__sctp, controlling=controlling)
        self.__sctpEndpoint = sctp.Endpoint(
            is_server=not controlling,
            transport=self.__sctp._dtlsSession.data)
        self.__datachannelManager = DataChannelManager(self, self.__sctpEndpoint)

    def __createSdp(self):
        ntp_seconds = get_ntp_seconds()
        sdp = [
            'v=0',
            'o=- %d %d IN IP4 0.0.0.0' % (ntp_seconds, ntp_seconds),
            's=-',
            't=0 0',
        ]

        for transceiver in self.__transceivers:
            iceConnection = transceiver._iceConnection
            default_candidate = iceConnection.get_default_candidate(1)
            if default_candidate is None:
                default_candidate = DUMMY_CANDIDATE
            sdp += [
                'm=%s %d UDP/TLS/RTP/SAVPF %s' % (
                    transceiver._kind,
                    default_candidate.port,
                    ' '.join([str(c.pt) for c in transceiver._codecs])),
                'c=IN IP4 %s' % default_candidate.host,
                'a=rtcp:9 IN IP4 0.0.0.0',
                'a=rtcp-mux',
            ]
            sdp += transport_sdp(iceConnection, transceiver._dtlsSession)
            sdp += ['a=%s' % transceiver.direction]
            sdp += ['a=ssrc:%d cname:%s' % (transceiver.sender._ssrc, self.__cname)]

            for codec in transceiver._codecs:
                sdp += ['a=rtpmap:%d %s' % (codec.pt, str(codec))]

        if self.__sctp:
            iceConnection = self.__sctp._iceConnection
            default_candidate = iceConnection.get_default_candidate(1)
            if default_candidate is None:
                default_candidate = DUMMY_CANDIDATE
            sdp += [
                'm=application %d DTLS/SCTP 5000' % default_candidate.port,
                'c=IN IP4 %s' % default_candidate.host,
            ]
            sdp += transport_sdp(iceConnection, self.__sctp._dtlsSession)
            sdp += ['a=sctpmap:5000 webrtc-datachannel 256']

        return '\r\n'.join(sdp) + '\r\n'

    def __createTransceiver(self, controlling, kind, sender_track=None):
        transceiver = RTCRtpTransceiver(
            sender=RTCRtpSender(kind=kind),
            receiver=RTCRtpReceiver(kind=kind))
        transceiver._kind = kind
        transceiver.sender._track = sender_track
        self.__createTransport(transceiver, controlling=controlling)
        self.__transceivers.append(transceiver)
        return transceiver

    def __createTransport(self, transceiver, controlling):
        transceiver._iceConnection = aioice.Connection(ice_controlling=controlling)
        transceiver._dtlsSession = dtls.DtlsSrtpSession(
            self.__dtlsContext,
            is_server=controlling,
            transport=transceiver._iceConnection)

    def __setIceConnectionState(self, state):
        self.__iceConnectionState = state
        self.emit('iceconnectionstatechange')

    def __setIceGatheringState(self, state):
        self.__iceGatheringState = state
        self.emit('icegatheringstatechange')

    def __setSignalingState(self, state):
        self.__signalingState = state
        self.emit('signalingstatechange')

    def __transports(self):
        for transceiver in self.__transceivers:
            yield transceiver._iceConnection, transceiver._dtlsSession
        if self.__sctp:
            yield self.__sctp._iceConnection, self.__sctp._dtlsSession
