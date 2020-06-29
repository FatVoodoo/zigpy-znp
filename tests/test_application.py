import asyncio
import logging

import pytest

try:
    # Python 3.8 already has this
    from mock import AsyncMock as CoroutineMock
except ImportError:
    from asynctest import CoroutineMock

import zigpy
import zigpy_znp.types as t
import zigpy_znp.commands as c
import zigpy_znp.config as conf

import zigpy.device
from zigpy.zdo.types import ZDOCmd, SizePrefixedSimpleDescriptor

from zigpy_znp.uart import ZnpMtProtocol

from zigpy_znp.api import ZNP
from zigpy_znp.uart import connect as uart_connect
from zigpy_znp.types.nvids import NwkNvIds
from zigpy_znp.zigbee.application import ControllerApplication


from test_api import (  # noqa: F401
    pytest_mark_asyncio_timeout,
    config_for_port_path,
    pingable_serial_port,
)

LOGGER = logging.getLogger(__name__)


class ForwardingTransport:
    class serial:
        name = "/dev/passthrough"
        baudrate = 45678

    def __init__(self, protocol):
        self.protocol = protocol

    def write(self, data):
        LOGGER.debug("Sending data %s to %s via %s", data, self.protocol, self)
        self.protocol.data_received(data)

    def close(self, exc=None):
        self.protocol.connection_lost(exc)

    def __repr__(self):
        return f"<{type(self).__name__} for {self.protocol}>"


class ServerZNP(ZNP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # We just respond to pings, nothing more.
        # XXX: the lambda allows us to replace `ping_replier` if necessary
        self.callback_for_response(c.SYS.Ping.Req(), lambda r: self.ping_replier(r))
        self.callback_for_response(
            c.SYS.Version.Req(), lambda r: self.version_replier(r)
        )

    def ping_replier(self, request):
        self.send(c.SYS.Ping.Rsp(Capabilities=t.MTCapabilities(1625)))

    def version_replier(self, request):
        self.send(
            c.SYS.Version.Rsp(
                TransportRev=2,
                ProductId=1,
                MajorRel=2,
                MinorRel=7,
                MaintRel=1,
                CodeRevision=20200417,
                BootloaderBuildType=c.sys.BootloaderBuildType.NON_BOOTLOADER_BUILD,
                BootloaderRevision=0xFFFFFFFF,
            )
        )

    def reply_once_to(self, request, responses):
        called_future = asyncio.get_running_loop().create_future()

        async def callback(request):
            if callback.called:
                return

            callback.called = True

            for response in responses:
                await asyncio.sleep(0.001)
                LOGGER.debug("Replying to %s with %s", request, response)

                if callable(response):
                    self.send(response(request))
                else:
                    self.send(response)

            called_future.set_result(True)

        callback.called = False
        self.callback_for_response(request, lambda r: asyncio.create_task(callback(r)))

        return called_future

    def reply_to(self, request, responses):
        async def callback(request):
            callback.call_count += 1

            for response in responses:
                await asyncio.sleep(0.001)
                LOGGER.debug("Replying to %s with %s", request, response)

                if callable(response):
                    self.send(response(request))
                else:
                    self.send(response)

        callback.call_count = 0

        self.callback_for_response(request, lambda r: asyncio.create_task(callback(r)))

        return callback

    def send(self, response):
        self._uart.send(response.to_frame())


@pytest.fixture
async def znp_server(mocker):
    device = "/dev/ttyFAKE0"
    config = config_for_port_path(device)

    server_znp = ServerZNP(config)
    server_znp._uart = ZnpMtProtocol(server_znp)

    def passthrough_serial_conn(loop, protocol_factory, url, *args, **kwargs):
        fut = loop.create_future()
        assert url == device

        client_protocol = protocol_factory()

        # Client writes go to the server
        client_transport = ForwardingTransport(server_znp._uart)

        # Server writes go to the client
        server_transport = ForwardingTransport(client_protocol)

        # Once both are setup, notify each one of their transport
        server_znp._uart.connection_made(server_transport)
        client_protocol.connection_made(client_transport)

        fut.set_result((client_transport, client_protocol))

        return fut

    mocker.patch("serial_asyncio.create_serial_connection", new=passthrough_serial_conn)

    return server_znp


@pytest.fixture
def application(znp_server):
    app = ControllerApplication(config_for_port_path("/dev/ttyFAKE0"))

    # Handle the entire startup sequence
    znp_server.reply_to(
        request=c.SYS.ResetReq.Req(Type=t.ResetType.Soft),
        responses=[
            c.SYS.ResetInd.Callback(
                Reason=t.ResetReason.PowerUp,
                TransportRev=2,
                ProductId=1,
                MajorRel=2,
                MinorRel=7,
                MaintRel=1,
            )
        ],
    )

    active_eps = [100, 13, 12, 11, 8, 1]

    znp_server.reply_to(
        request=c.ZDO.ActiveEpReq.Req(DstAddr=0x0000, NWKAddrOfInterest=0x0000),
        responses=[
            c.ZDO.ActiveEpReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.ActiveEpRsp.Callback(
                Src=0x0000,
                Status=t.ZDOStatus.SUCCESS,
                NWK=0x0000,
                ActiveEndpoints=active_eps,
            ),
        ],
    )

    znp_server.reply_to(
        request=c.ZDO.ActiveEpReq.Req(DstAddr=0x0000, NWKAddrOfInterest=0x0000),
        responses=[
            c.ZDO.ActiveEpReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.ActiveEpRsp.Callback(
                Src=0x0000,
                Status=t.ZDOStatus.SUCCESS,
                NWK=0x0000,
                ActiveEndpoints=active_eps,
            ),
        ],
    )

    def on_endpoint_registration(req):
        assert req.Endpoint not in active_eps

        active_eps.append(req.Endpoint)
        active_eps.sort(reverse=True)

        return c.AF.Register.Rsp(Status=t.Status.SUCCESS)

    znp_server.reply_to(
        request=c.AF.Register.Req(partial=True), responses=[on_endpoint_registration],
    )

    def on_endpoint_deletion(req):
        assert req.Endpoint in active_eps

        active_eps.remove(req.Endpoint)

        return c.AF.Delete.Rsp(Status=t.Status.SUCCESS)

    znp_server.reply_to(
        request=c.AF.Delete.Req(partial=True), responses=[on_endpoint_deletion],
    )

    znp_server.reply_to(
        request=c.AppConfig.BDBStartCommissioning.Req(
            Mode=c.app_config.BDBCommissioningMode.NwkFormation
        ),
        responses=[
            c.AppConfig.BDBStartCommissioning.Rsp(Status=t.Status.SUCCESS),
            c.AppConfig.BDBCommissioningNotification.Callback(
                Status=c.app_config.BDBCommissioningStatus.Success,
                Mode=c.app_config.BDBCommissioningMode.NwkSteering,
                RemainingModes=c.app_config.BDBCommissioningMode.NONE,
            ),
        ],
    )

    # Reply to the initialization NVID writes
    for nvid in [
        NwkNvIds.CONCENTRATOR_ENABLE,
        NwkNvIds.CONCENTRATOR_DISCOVERY,
        NwkNvIds.CONCENTRATOR_RC,
        NwkNvIds.SRC_RTG_EXPIRY_TIME,
        NwkNvIds.NWK_CHILD_AGE_ENABLE,
        NwkNvIds.LOGICAL_TYPE,
    ]:
        znp_server.reply_to(
            request=c.SYS.OSALNVWrite.Req(Id=nvid, Offset=0, partial=True),
            responses=[c.SYS.OSALNVWrite.Rsp(Status=t.Status.SUCCESS)],
        )

    znp_server.reply_to(
        request=c.SYS.OSALNVRead.Req(Id=NwkNvIds.HAS_CONFIGURED_ZSTACK3, Offset=0),
        responses=[c.SYS.OSALNVRead.Rsp(Status=t.Status.SUCCESS, Value=b"\x55")],
    )

    znp_server.reply_to(
        request=c.Util.GetDeviceInfo.Req(),
        responses=[
            c.Util.GetDeviceInfo.Rsp(
                Status=t.Status.SUCCESS,
                IEEE=t.EUI64([0x00, 0x12, 0x4B, 0x00, 0x1C, 0xAA, 0xAC, 0x5C]),
                NWK=t.NWK(0xFFFE),
                DeviceType=t.DeviceTypeCapabilities(7),
                DeviceState=t.DeviceState.InitializedNotStarted,
                AssociatedDevices=[],
            )
        ],
    )

    znp_server.reply_to(
        request=c.ZDO.StartupFromApp.Req(partial=True),
        responses=[
            c.ZDO.StartupFromApp.Rsp(State=c.zdo.StartupState.RestoredNetworkState),
            c.ZDO.StateChangeInd.Callback(State=t.DeviceState.StartedAsCoordinator),
        ],
    )

    # The NIB matches the above device info
    NIB = bytes.fromhex(
        """
        790502331433001e0000000105018f00070002051e000000190000000000000000000000FFFE0800
        008010020f0f040001000000010000000000124b001caaac5c010000000000000000000000000000
        000000000000000000000000000000000000000000000f030001780a0100000020470000
        """
    )

    znp_server.reply_to(
        request=c.SYS.OSALNVRead.Req(Id=NwkNvIds.NIB, Offset=0),
        responses=[c.SYS.OSALNVRead.Rsp(Status=t.Status.SUCCESS, Value=NIB)],
    )

    return app, znp_server


@pytest_mark_asyncio_timeout(seconds=5)
async def test_application_startup_skip_bootloader(application, mocker):
    app, znp_server = application

    first_uart_byte = None

    def create_patched_write(original_write):
        def patched_write(data):
            nonlocal first_uart_byte

            # Intercept the first byte if it's destined for the bootloader
            is_for_bootloader = data[0] in c.ubl.BootloaderRunMode._value2member_map_

            if first_uart_byte is None and is_for_bootloader:
                first_uart_byte = data[0]
                data = data[1:]

            return original_write(data)

        return patched_write

    async def patched_uart_connect(config, api):
        protocol = await uart_connect(config, api)
        protocol.transport.write = create_patched_write(protocol.transport.write)

        return protocol

    mocker.patch("zigpy_znp.uart.connect", side_effect=patched_uart_connect)

    app.update_config({conf.CONF_ZNP_CONFIG: {conf.CONF_SKIP_BOOTLOADER: True}})
    await app.startup(auto_form=False)

    assert first_uart_byte == c.ubl.BootloaderRunMode.FORCE_RUN


@pytest_mark_asyncio_timeout(seconds=5)
async def test_application_startup_nib(application):
    app, znp_server = application

    await app.startup(auto_form=False)

    # This is read from the NIB on startup
    assert app.channel == 25
    assert app.channels == t.Channels.from_channel_list([15, 20, 25])


@pytest_mark_asyncio_timeout(seconds=5)
async def test_application_startup_endpoints(application):
    app, znp_server = application

    endpoints = []
    znp_server.callback_for_response(c.AF.Register.Req(partial=True), endpoints.append)

    await app.startup(auto_form=False)

    assert len(endpoints) == 6


@pytest_mark_asyncio_timeout(seconds=5)
async def test_application_startup_failure(application):
    app, znp_server = application

    # Prevent the fixture's default response
    znp_server._response_listeners[c.SYS.OSALNVRead.Req.header].clear()

    znp_server.reply_once_to(
        request=c.SYS.OSALNVRead.Req(Id=NwkNvIds.HAS_CONFIGURED_ZSTACK3, Offset=0),
        responses=[c.SYS.OSALNVRead.Rsp(Status=t.Status.INVALID_PARAMETER, Value=b"")],
    )

    # We cannot start the application if Z-Stack is not configured and without auto_form
    with pytest.raises(RuntimeError):
        await app.startup(auto_form=False)

    znp_server.reply_once_to(
        request=c.SYS.OSALNVRead.Req(Id=NwkNvIds.HAS_CONFIGURED_ZSTACK3, Offset=0),
        responses=[c.SYS.OSALNVRead.Rsp(Status=t.Status.SUCCESS, Value=b"\x00")],
    )

    with pytest.raises(RuntimeError):
        await app.startup(auto_form=False)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_application_startup_tx_power(application):
    app, znp_server = application

    set_tx_power = znp_server.reply_once_to(
        request=c.SYS.SetTxPower.Req(TXPower=19),
        responses=[c.SYS.SetTxPower.Rsp(Status=t.Status.SUCCESS)],
    )

    app.update_config({conf.CONF_ZNP_CONFIG: {conf.CONF_TX_POWER: 19}})

    await app.startup(auto_form=False)
    await set_tx_power


@pytest_mark_asyncio_timeout(seconds=3)
async def test_permit_join(application):
    app, znp_server = application

    # Handle the ZDO broadcast sent by Zigpy
    data_req_sent = znp_server.reply_once_to(
        request=c.AF.DataRequestExt.Req(partial=True, SrcEndpoint=0, DstEndpoint=0),
        responses=[
            c.AF.DataRequestExt.Rsp(Status=t.Status.SUCCESS),
            c.AF.DataConfirm.Callback(Status=t.Status.SUCCESS, Endpoint=0, TSN=1),
        ],
    )

    # Handle the permit join request sent by us
    permit_join_sent = znp_server.reply_once_to(
        request=c.ZDO.MgmtPermitJoinReq.Req(partial=True),
        responses=[
            c.ZDO.MgmtPermitJoinReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.MgmtPermitJoinRsp.Callback(Src=0x0000, Status=t.ZDOStatus.SUCCESS),
        ],
    )

    await app.startup(auto_form=False)
    await app.permit(time_s=10)

    # Make sure both commands were received
    await asyncio.gather(data_req_sent, permit_join_sent)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_permit_join_failure(application):
    app, znp_server = application

    # Handle the ZDO broadcast sent by Zigpy
    data_req_sent = znp_server.reply_once_to(
        request=c.AF.DataRequestExt.Req(partial=True, SrcEndpoint=0, DstEndpoint=0),
        responses=[
            c.AF.DataRequestExt.Rsp(Status=t.Status.SUCCESS),
            c.AF.DataConfirm.Callback(Status=t.Status.SUCCESS, Endpoint=0, TSN=1),
        ],
    )

    # Handle the permit join request sent by us
    permit_join_sent = znp_server.reply_once_to(
        request=c.ZDO.MgmtPermitJoinReq.Req(partial=True),
        responses=[
            c.ZDO.MgmtPermitJoinReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.MgmtPermitJoinRsp.Callback(Src=0xFFFF, Status=t.ZDOStatus.TIMEOUT),
        ],
    )

    await app.startup(auto_form=False)

    with pytest.raises(RuntimeError):
        await app.permit(time_s=10)

    # Make sure both commands were received
    await asyncio.gather(data_req_sent, permit_join_sent)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_on_zdo_relays_message_callback(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    device = mocker.Mock()
    mocker.patch.object(app, "get_device", return_value=device)

    znp_server.send(c.ZDO.SrcRtgInd.Callback(DstAddr=0x1234, Relays=[0x5678, 0xABCD]))
    assert device.relays == [0x5678, 0xABCD]


@pytest_mark_asyncio_timeout(seconds=3)
async def test_on_zdo_device_announce(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    mocker.patch.object(app, "handle_message")

    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xFA9E)

    znp_server.send(
        c.ZDO.EndDeviceAnnceInd.Callback(
            Src=0x0001,
            NWK=device.nwk,
            IEEE=device.ieee,
            Capabilities=c.zdo.MACCapabilities.Router,
        )
    )

    app.handle_message.called_once_with(cluster=ZDOCmd.Device_annce)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_on_zdo_device_join(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    mocker.patch.object(app, "handle_join")

    nwk = 0x1234
    ieee = t.EUI64(range(8))

    znp_server.send(c.ZDO.TCDevInd.Callback(SrcNwk=nwk, SrcIEEE=ieee, ParentNwk=0x0001))
    app.handle_join.assert_called_once_with(nwk=nwk, ieee=ieee, parent_nwk=0x0001)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_on_zdo_device_leave_callback(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    mocker.patch.object(app, "handle_leave")

    nwk = 0x1234
    ieee = t.EUI64(range(8))

    znp_server.send(
        c.ZDO.LeaveInd.Callback(
            NWK=nwk, IEEE=ieee, Request=False, Remove=False, Rejoin=False
        )
    )
    app.handle_leave.assert_called_once_with(nwk=nwk, ieee=ieee)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_on_af_message_callback(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    device = mocker.Mock()
    mocker.patch.object(
        app, "get_device", side_effect=[device, KeyError("No such device")]
    )
    mocker.patch.object(app, "handle_message")

    af_message = c.AF.IncomingMsg.Callback(
        GroupId=1,
        ClusterId=2,
        SrcAddr=0xABCD,
        SrcEndpoint=4,
        DstEndpoint=5,
        WasBroadcast=False,
        LQI=19,
        SecurityUse=False,
        TimeStamp=0,
        TSN=0,
        Data=b"test",
        MacSrcAddr=0x0000,
        MsgResultRadius=1,
    )

    # Normal message
    znp_server.send(af_message)
    app.get_device.assert_called_once_with(nwk=0xABCD)
    device.radio_details.assert_called_once_with(lqi=19, rssi=None)
    app.handle_message.assert_called_once_with(
        sender=device, profile=260, cluster=2, src_ep=4, dst_ep=5, message=b"test"
    )

    device.reset_mock()
    app.handle_message.reset_mock()
    app.get_device.reset_mock()

    # Message from an unknown device
    znp_server.send(af_message)
    app.get_device.assert_called_once_with(nwk=0xABCD)
    assert device.radio_details.call_count == 0
    assert app.handle_message.call_count == 0


@pytest_mark_asyncio_timeout(seconds=3)
async def test_probe(pingable_serial_port):  # noqa: F811
    assert not (
        await ControllerApplication.probe(
            conf.SCHEMA_DEVICE({conf.CONF_DEVICE_PATH: "/dev/null"})
        )
    )

    assert await ControllerApplication.probe(
        conf.SCHEMA_DEVICE({conf.CONF_DEVICE_PATH: pingable_serial_port})
    )


@pytest_mark_asyncio_timeout(seconds=5)
async def test_reconnect(event_loop, application):
    app, znp_server = application
    app._config[conf.CONF_ZNP_CONFIG][conf.CONF_AUTO_RECONNECT_RETRY_DELAY] = 0.01

    await app.startup(auto_form=False)

    # Don't reply to the ping request this time
    old_ping_replier = znp_server.ping_replier
    znp_server.ping_replier = lambda request: None

    # Now that we're connected, close the connection due to an error
    SREQ_TIMEOUT = 0.2
    app._config[conf.CONF_ZNP_CONFIG][conf.CONF_SREQ_TIMEOUT] = SREQ_TIMEOUT
    app._znp._uart.connection_lost(RuntimeError("Uh oh"))
    app.connection_lost(RuntimeError("Uh oh"))

    assert app._znp is None

    # Wait for the SREQ_TIMEOUT to pass, we should fail to reconnect
    await asyncio.sleep(SREQ_TIMEOUT + 0.1)
    assert app._znp is None

    # Respond to the ping appropriately
    znp_server.ping_replier = old_ping_replier

    # Our reconnect task should complete after we send the ping reply
    reconnect_fut = event_loop.create_future()
    app._reconnect_task.add_done_callback(lambda _: reconnect_fut.set_result(None))

    # We should be reconnected soon and the app should have been restarted
    await reconnect_fut
    assert app._znp is not None
    assert app._znp._uart is not None


@pytest_mark_asyncio_timeout(seconds=3)
async def test_auto_connect(mocker, application):
    AUTO_DETECTED_PORT = "/dev/ttyFAKE0"

    app, znp_server = application

    uart_guess_port = mocker.patch(
        "zigpy_znp.uart.guess_port", return_value=AUTO_DETECTED_PORT
    )

    async def fixed_uart_connect(config, api):
        protocol = await uart_connect(config, api)
        protocol.transport.serial.name = AUTO_DETECTED_PORT

        return protocol

    uart_connect_mock = mocker.patch(
        "zigpy_znp.uart.connect", side_effect=fixed_uart_connect
    )

    app._config[conf.CONF_DEVICE][conf.CONF_DEVICE_PATH] = "auto"
    await app.startup(auto_form=False)

    assert uart_guess_port.call_count == 1
    assert uart_connect_mock.call_count == 1
    assert app._config[conf.CONF_DEVICE][conf.CONF_DEVICE_PATH] == AUTO_DETECTED_PORT


@pytest_mark_asyncio_timeout(seconds=3)
async def test_close(mocker, application):
    app, znp_server = application
    app.connection_lost = mocker.MagicMock(wraps=app.connection_lost)

    await app.startup(auto_form=False)
    app._znp._uart.connection_lost(None)

    app.connection_lost.assert_called_once_with(None)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_shutdown(mocker, application):
    app, znp_server = application

    await app.startup(auto_form=False)

    mocker.patch.object(app, "_reconnect_task")
    mocker.patch.object(app, "_znp")

    await app.shutdown()

    app._reconnect_task.cancel.assert_called_once_with()
    app._znp.close.assert_called_once_with()


@pytest_mark_asyncio_timeout(seconds=3)
async def test_zdo_request_interception(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xFA9E)

    # Send back a request response
    active_ep_req = znp_server.reply_once_to(
        request=c.ZDO.SimpleDescReq.Req(
            DstAddr=device.nwk, NWKAddrOfInterest=device.nwk, Endpoint=1
        ),
        responses=[
            c.ZDO.SimpleDescReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.SimpleDescRsp.Callback(
                Src=device.nwk,
                Status=t.ZDOStatus.SUCCESS,
                NWK=device.nwk,
                SimpleDescriptor=SizePrefixedSimpleDescriptor(
                    *dict(
                        endpoint=1,
                        profile=49246,
                        device_type=256,
                        device_version=2,
                        input_clusters=[0, 3, 4, 5, 6, 8, 2821, 4096],
                        output_clusters=[5, 25, 32, 4096],
                    ).values()
                ),
            ),
        ],
    )

    status, message = await app.request(
        device=device,
        profile=260,
        cluster=ZDOCmd.Simple_Desc_req,
        src_ep=0,
        dst_ep=0,
        sequence=1,
        data=b"\x01\x9e\xfa\x01",
        use_ieee=False,
    )

    await active_ep_req

    assert status == t.Status.SUCCESS


@pytest_mark_asyncio_timeout(seconds=10)
async def test_zigpy_request(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    TSN = 1

    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xAABB)
    device.status = zigpy.device.Status.ENDPOINTS_INIT
    device.initializing = False

    device.add_endpoint(1).add_input_cluster(6)

    # Respond to a light turn on request
    data_req = znp_server.reply_once_to(
        request=c.AF.DataRequestExt.Req(
            DstAddrModeAddress=t.AddrModeAddress(
                mode=t.AddrMode.NWK, address=device.nwk
            ),
            DstEndpoint=1,
            SrcEndpoint=1,
            ClusterId=6,
            TSN=TSN,
            Data=bytes([0x01, TSN, 0x01]),
            partial=True,
        ),
        responses=[
            c.AF.DataRequestExt.Rsp(Status=t.Status.SUCCESS),
            c.AF.DataConfirm.Callback(Status=t.Status.SUCCESS, Endpoint=1, TSN=TSN,),
            c.ZDO.SrcRtgInd.Callback(DstAddr=device.nwk, Relays=[]),
            c.AF.IncomingMsg.Callback(
                GroupId=0x0000,
                ClusterId=6,
                SrcAddr=device.nwk,
                SrcEndpoint=1,
                DstEndpoint=1,
                WasBroadcast=False,
                LQI=63,
                SecurityUse=False,
                TimeStamp=1198515,
                TSN=0,
                Data=bytes([0x08, TSN, 0x0B, 0x00, 0x00]),
                MacSrcAddr=device.nwk,
                MsgResultRadius=29,
            ),
        ],
    )

    # Turn on the light
    await device.endpoints[1].on_off.on()
    await data_req


@pytest_mark_asyncio_timeout(seconds=10)
async def test_zigpy_request_failure(application, mocker):
    app, znp_server = application
    await app.startup(auto_form=False)

    TSN = 1

    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xAABB)
    device.status = zigpy.device.Status.ENDPOINTS_INIT
    device.initializing = False

    device.add_endpoint(1).add_input_cluster(6)

    # Fail to respond to a light turn on request
    znp_server.reply_to(
        request=c.AF.DataRequestExt.Req(
            DstAddrModeAddress=t.AddrModeAddress(
                mode=t.AddrMode.NWK, address=device.nwk
            ),
            DstEndpoint=1,
            SrcEndpoint=1,
            ClusterId=6,
            TSN=TSN,
            Data=bytes([0x01, TSN, 0x01]),
            partial=True,
        ),
        responses=[
            c.AF.DataRequestExt.Rsp(Status=t.Status.SUCCESS),
            c.AF.DataConfirm.Callback(Status=t.Status.FAILURE, Endpoint=1, TSN=TSN,),
        ],
    )

    mocker.spy(app, "_send_request")

    # Fail to turn on the light
    with pytest.raises(zigpy.exceptions.DeliveryError):
        await device.endpoints[1].on_off.on()

    assert app._send_request.call_count == 1


@pytest_mark_asyncio_timeout(seconds=3)
@pytest.mark.parametrize(
    "use_ieee,dev_addr",
    [
        (True, t.AddrModeAddress(mode=t.AddrMode.IEEE, address=t.EUI64(range(8)))),
        (False, t.AddrModeAddress(mode=t.AddrMode.NWK, address=t.NWK(0xAABB))),
    ],
)
async def test_request_use_ieee(application, mocker, use_ieee, dev_addr):
    app, znp_server = application
    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xAABB)

    mocker.patch.object(app, "_send_request", new=CoroutineMock())

    await app.request(
        device,
        use_ieee=use_ieee,
        profile=1,
        cluster=2,
        src_ep=3,
        dst_ep=4,
        sequence=5,
        data=b"6",
    )

    assert app._send_request.call_count == 1
    assert app._send_request.mock_calls[0][2]["dst_addr"] == dev_addr


@pytest_mark_asyncio_timeout(seconds=3)
async def test_update_network_noop(mocker, application):
    app, znp_server = application

    await app.startup(auto_form=False)

    app._znp = mocker.NonCallableMock()

    # Nothing should be called
    await app.update_network(reset=False)

    # This will call _znp.request and fail
    with pytest.raises(TypeError):
        await app.update_network(reset=True)


@pytest_mark_asyncio_timeout(seconds=5)
async def test_update_network(mocker, caplog, application):
    app, znp_server = application

    await app.startup(auto_form=False)
    mocker.spy(app, "_reset")

    channel = t.uint8_t(15)
    pan_id = t.PanId(0x1234)
    extended_pan_id = t.ExtendedPanId(range(8))
    channels = t.Channels.from_channel_list([11, 15, 20])
    network_key = t.KeyData(range(16))

    channels_updated = znp_server.reply_once_to(
        request=c.Util.SetChannels.Req(Channels=channels),
        responses=[c.Util.SetChannels.Rsp(Status=t.Status.SUCCESS)],
    )

    bdb_set_primary_channel = znp_server.reply_once_to(
        request=c.AppConfig.BDBSetChannel.Req(IsPrimary=True, Channel=channels),
        responses=[c.AppConfig.BDBSetChannel.Rsp(Status=t.Status.SUCCESS)],
    )

    bdb_set_secondary_channel = znp_server.reply_once_to(
        request=c.AppConfig.BDBSetChannel.Req(
            IsPrimary=False, Channel=t.Channels.NO_CHANNELS
        ),
        responses=[c.AppConfig.BDBSetChannel.Rsp(Status=t.Status.SUCCESS)],
    )

    set_pan_id = znp_server.reply_once_to(
        request=c.Util.SetPanId.Req(PanId=pan_id),
        responses=[c.Util.SetPanId.Rsp(Status=t.Status.SUCCESS)],
    )

    set_extended_pan_id = znp_server.reply_once_to(
        request=c.SYS.OSALNVWrite.Req(
            Id=NwkNvIds.EXTENDED_PAN_ID, Offset=0, Value=extended_pan_id.serialize()
        ),
        responses=[c.SYS.OSALNVWrite.Rsp(Status=t.Status.SUCCESS)],
    )

    set_network_key_util = znp_server.reply_once_to(
        request=c.Util.SetPreConfigKey.Req(PreConfigKey=network_key),
        responses=[c.Util.SetPreConfigKey.Rsp(Status=t.Status.SUCCESS)],
    )

    set_network_key_nvram = znp_server.reply_once_to(
        request=c.SYS.OSALNVWrite.Req(
            Id=NwkNvIds.PRECFGKEYS_ENABLE, Offset=0, Value=t.Bool(True).serialize()
        ),
        responses=[c.SYS.OSALNVWrite.Rsp(Status=t.Status.SUCCESS)],
    )

    set_nib_nvram = znp_server.reply_once_to(
        request=c.SYS.OSALNVWrite.Req(Id=NwkNvIds.NIB, Offset=0, partial=True),
        responses=[c.SYS.OSALNVWrite.Rsp(Status=t.Status.SUCCESS)],
    )

    # But it does succeed with a warning if you explicitly allow it
    with caplog.at_level(logging.WARNING):
        await app.update_network(
            channel=channel,
            channels=channels,
            extended_pan_id=extended_pan_id,
            network_key=network_key,
            pan_id=pan_id,
            tc_address=t.EUI64(range(8)),
            tc_link_key=t.KeyData(range(8)),
            update_id=0,
            reset=True,
        )

    # We should receive a few warnings for `tc_` stuff
    assert len(caplog.records) >= 2

    await channels_updated
    await bdb_set_primary_channel
    await bdb_set_secondary_channel
    await set_pan_id
    await set_extended_pan_id
    await set_network_key_util
    await set_network_key_nvram
    await set_nib_nvram

    app._reset.assert_called_once_with()

    # Ensure we set everything we could
    assert app.nwk_update_id is None  # We can't use it
    assert app.channel == channel
    assert app.channels == channels
    assert app.pan_id == pan_id
    assert app.extended_pan_id == extended_pan_id


@pytest_mark_asyncio_timeout(seconds=5)
async def test_update_network_bad_channel(mocker, caplog, application):
    app, znp_server = application

    with pytest.raises(ValueError):
        # 12 is not in the mask
        await app.update_network(
            channel=t.uint8_t(12), channels=t.Channels.from_channel_list([11, 15, 20]),
        )


@pytest_mark_asyncio_timeout(seconds=3)
async def test_force_remove(application, mocker):
    app, znp_server = application

    await app.startup(auto_form=False)

    mocker.patch("zigpy_znp.zigbee.application.ZDO_REQUEST_TIMEOUT", new=0.3)

    device = app.add_device(ieee=t.EUI64(range(8)), nwk=0xAABB)
    device.status = zigpy.device.Status.ENDPOINTS_INIT
    device.initializing = False

    # Reply to zigpy's leave request
    bad_mgmt_leave_req = znp_server.reply_once_to(
        request=c.ZDO.MgmtLeaveReq.Req(DstAddr=device.nwk, partial=True),
        responses=[c.ZDO.MgmtLeaveReq.Rsp(Status=t.Status.FAILURE)],
    )

    # Reply to our own leave request
    good_mgmt_leave_req = znp_server.reply_once_to(
        request=c.ZDO.MgmtLeaveReq.Req(DstAddr=0x0000, partial=True),
        responses=[
            c.ZDO.MgmtLeaveReq.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.MgmtLeaveRsp.Callback(Src=0x000, Status=t.ZDOStatus.SUCCESS),
        ],
    )

    # Make sure the device exists
    assert app.get_device(nwk=device.nwk) is device

    await app.remove(device.ieee)
    await asyncio.gather(bad_mgmt_leave_req, good_mgmt_leave_req)

    # Make sure the device is gone once we remove it
    with pytest.raises(KeyError):
        app.get_device(nwk=device.nwk)


@pytest_mark_asyncio_timeout(seconds=3)
async def test_auto_form_unnecessary(application, mocker):
    app, znp_server = application

    mocker.patch.object(app, "form_network", new=CoroutineMock())

    await app.startup(auto_form=True)
    assert app.form_network.call_count == 0


@pytest_mark_asyncio_timeout(seconds=3)
async def test_auto_form_necessary(application, mocker):
    app, znp_server = application
    nvram = {}

    mocker.patch.object(app, "update_network", new=CoroutineMock())
    mocker.spy(app, "_reset")

    def nvram_writer(req):
        nvram[req.Id] = req.Value

        return c.SYS.OSALNVWrite.Rsp(Status=t.Status.SUCCESS)

    def nvram_init(req):
        nvram[req.Id] = req.Value

        return c.SYS.OSALNVItemInit.Rsp(Status=t.Status.SUCCESS)

    # Prevent the fixture's default NVRAM responses, except for the NIB
    listeners = znp_server._response_listeners[c.SYS.OSALNVRead.Req.header]
    znp_server._response_listeners[c.SYS.OSALNVRead.Req.header] = [
        listener
        for listener in listeners
        if listener.matching_commands[0]
        == c.SYS.OSALNVRead.Req(Id=NwkNvIds.NIB, Offset=0)
    ]

    read_zstack_configured = znp_server.reply_once_to(
        request=c.SYS.OSALNVRead.Req(Id=NwkNvIds.HAS_CONFIGURED_ZSTACK3, Offset=0),
        responses=[c.SYS.OSALNVRead.Rsp(Status=t.Status.INVALID_PARAMETER, Value=b"")],
    )

    znp_server.reply_to(
        request=c.SYS.OSALNVWrite.Req(Offset=0, partial=True), responses=[nvram_writer]
    )

    znp_server.reply_to(
        request=c.SYS.OSALNVItemInit.Req(partial=True), responses=[nvram_init]
    )

    znp_server.reply_to(
        request=c.AppConfig.BDBStartCommissioning.Req(
            Mode=c.app_config.BDBCommissioningMode.NwkFormation
        ),
        responses=[
            c.AppConfig.BDBStartCommissioning.Rsp(Status=t.Status.SUCCESS),
            c.ZDO.StateChangeInd.Callback(State=t.DeviceState.StartedAsCoordinator),
        ],
    )

    znp_server.reply_to(
        request=c.AppConfig.BDBStartCommissioning.Req(
            Mode=c.app_config.BDBCommissioningMode.NwkSteering
        ),
        responses=[c.AppConfig.BDBStartCommissioning.Rsp(Status=t.Status.SUCCESS)],
    )

    await app.startup(auto_form=True)

    await read_zstack_configured

    assert app.update_network.call_count == 1
    assert app._reset.call_count == 2

    assert nvram[NwkNvIds.HAS_CONFIGURED_ZSTACK3] == b"\x55"
    assert nvram[NwkNvIds.STARTUP_OPTION] == t.StartupOptions.ClearState.serialize()
    assert nvram[NwkNvIds.LOGICAL_TYPE] == t.DeviceLogicalType.Coordinator.serialize()
    assert nvram[NwkNvIds.ZDO_DIRECT_CB] == t.Bool(True).serialize()
