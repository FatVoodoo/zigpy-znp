import enum
import logging

import attr
import zigpy.zdo.types
import zigpy_znp.types as t


LOGGER = logging.getLogger(__name__)


@attr.s
class BindEntry(t.Struct):
    """Bind table entry."""

    Src = attr.ib(type=t.EUI64, converter=t.Struct.converter(t.EUI64))
    SrcEp = attr.ib(type=t.uint8_t, converter=t.uint8_t)
    ClusterId = attr.ib(type=t.ClusterId, converter=t.ClusterId)
    DstAddr = attr.ib(
        type=zigpy.zdo.types.MultiAddress,
        converter=t.Struct.converter(zigpy.zdo.types.MultiAddress),
    )


class CommandType(enum.IntEnum):
    """Command Type."""

    POLL = 0
    SREQ = 1  # A synchronous request that requires an immediate response
    AREQ = 2  # An asynchronous request
    SRSP = 3  # A synchronous response. This type of command is only sent in response to
    # a SREQ command
    RESERVED_4 = 4
    RESERVED_5 = 5
    RESERVED_6 = 6
    RESERVED_7 = 7


class ErrorCode(t.uint8_t, enum.Enum):
    """Error code."""

    INVALID_SUBSYSTEM = 0x01
    INVALID_COMMAND_ID = 0x02
    INVALID_PARAMETER = 0x03
    INVALID_LENGTH = 0x04

    @classmethod
    def deserialize(cls, data, byteorder="little"):
        try:
            return super().deserialize(data, byteorder)
        except ValueError:
            code, data = t.uint8_t.deserialize(data, byteorder)
            fake_enum = t.FakeEnum("ErrorCode")
            return fake_enum(f"unknown_0x{code:02x}", code), data


class Subsystem(t.enum_uint8, enum.IntEnum):
    """Command subsystem."""

    Reserved = 0x00
    SYS = 0x01
    MAC = 0x02
    NWK = 0x03
    AF = 0x04
    ZDO = 0x05
    SAPI = 0x06
    UTIL = 0x07
    DEBUG = 0x08
    APP = 0x09
    RESERVED_10 = 0x0A
    RESERVED_11 = 0x0B
    RESERVED_12 = 0x0C
    RESERVED_13 = 0x0D
    RESERVED_14 = 0x0E
    APPConfig = 0x0F
    RESERVED_16 = 0x10
    RESERVED_17 = 0x11
    RESERVED_18 = 0x12
    RESERVED_19 = 0x13
    RESERVED_20 = 0x14
    ZGP = 0x15
    RESERVED_22 = 0x16
    RESERVED_23 = 0x17
    RESERVED_24 = 0x18
    RESERVED_25 = 0x19
    RESERVED_26 = 0x1A
    RESERVED_27 = 0x1B
    RESERVED_28 = 0x1C
    RESERVED_29 = 0x1D
    RESERVED_30 = 0x1E
    RESERVED_31 = 0x1F


class CallbackSubsystem(t.enum_uint16, enum.IntEnum):
    """Subscribe/unsubscribe subsystem callbacks."""

    MT_SYS = Subsystem.SYS << 8
    MC_MAC = Subsystem.MAC << 8
    MT_NWK = Subsystem.NWK << 8
    MT_AF = Subsystem.AF << 8
    MT_ZDO = Subsystem.ZDO << 8
    MT_SAPI = Subsystem.SAPI << 8
    MT_UTIL = Subsystem.UTIL << 8
    MT_DEBUG = Subsystem.DEBUG << 8
    MT_APP = Subsystem.APP << 8
    MT_APPConfig = Subsystem.APPConfig << 8
    MT_ZGP = Subsystem.ZGP << 8
    ALL = 0xFFFF


class CommandHeader(t.uint16_t):
    """CommandHeader class."""

    @property
    def cmd0(self) -> t.uint8_t:
        return t.uint8_t(self & 0x00FF)

    @property
    def id(self) -> t.uint8_t:
        """Return CommandHeader id."""
        return t.uint8_t(self >> 8)

    def with_id(self, value: int) -> "CommandHeader":
        """command ID setter."""
        return type(self)(self & 0x00FF | (value & 0xFF) << 8)

    @property
    def subsystem(self) -> Subsystem:
        """Return subsystem of the command."""
        return Subsystem(self.cmd0 & 0x1F)

    def with_subsystem(self, value: Subsystem) -> "CommandHeader":
        return type(self)(self & 0xFFE0 | value & 0x1F)

    @property
    def type(self) -> CommandType:
        """Return command type."""
        return CommandType(self.cmd0 >> 5)

    def with_type(self, value) -> "CommandHeader":
        return type(self)(self & 0xFF1F | (value & 0x07) << 5)


@attr.s
class CommandDef:
    command_type: CommandType = attr.ib()
    command_id: int = attr.ib()
    req_schema: t.Schema = attr.ib(factory=t.Schema)
    rsp_schema: t.Schema = attr.ib(factory=t.Schema)


class CommandsMeta(type):
    """
    Metaclass that creates `Command` subclasses out of the `CommandDef` definitions
    """

    def __new__(cls, name, bases, classdict, *, subsystem):
        # Ignore CommandsBase
        if not bases:
            return type.__new__(cls, name, bases, classdict)

        classdict["_commands"] = []

        for command, definition in classdict.items():
            if not isinstance(definition, CommandDef):
                continue

            # We manually create the qualname to match the final object structure
            qualname = classdict["__qualname__"] + "." + command

            # The commands class is dynamically created from the definition
            helper_class_dict = {
                "definition": definition,
                "type": definition.command_type,
                "subsystem": subsystem,
                "__qualname__": qualname,
            }

            header = (
                CommandHeader()
                .with_id(definition.command_id)
                .with_type(definition.command_type)
                .with_subsystem(subsystem)
            )

            if definition.command_type == CommandType.SREQ:
                req_header = header
                rsp_header = CommandHeader(0x0040 + req_header)

                class Req(CommandBase, header=req_header, schema=definition.req_schema):
                    pass

                class Rsp(CommandBase, header=rsp_header, schema=definition.rsp_schema):
                    pass

                Req.__qualname__ = qualname + ".Req"
                Req.Rsp = Rsp

                Rsp.__qualname__ = qualname + ".Rsp"
                Rsp.Req = Req

                helper_class_dict["Req"] = Req
                helper_class_dict["Rsp"] = Rsp
            elif definition.command_type == CommandType.AREQ:

                class Callback(
                    CommandBase, header=header, schema=definition.req_schema
                ):
                    pass

                Callback.__qualname__ = qualname + ".Callback"
                helper_class_dict["Callback"] = Callback
            else:
                raise ValueError(
                    f"Unknown command type: {definition.command_type}"
                )  # pragma: no cover

            classdict[command] = type(command, (), helper_class_dict)
            classdict["_commands"].append(classdict[command])

        return type.__new__(cls, name, bases, classdict)

    def __iter__(self):
        return iter(self._commands)


class CommandsBase(metaclass=CommandsMeta, subsystem=None):
    pass


class CommandBase:
    def __init_subclass__(cls, *, header, schema):
        super().__init_subclass__()
        cls.header = header
        cls.schema = schema

    def __init__(self, *, partial=False, **params):
        all_params = {param.name for param in self.schema.parameters}
        given_params = set(params.keys())

        if not partial and all_params - given_params:
            raise KeyError(f"Missing parameters: {all_params - given_params}")
        elif given_params - all_params:
            raise KeyError(
                f"Unexpected parameters: {given_params - all_params}. "
                f"Expected one of {all_params}"
            )

        bound_params = {}

        for param in self.schema.parameters:
            if partial and params.get(param.name) is None:
                bound_params[param.name] = (param, None)
                continue

            value = params[param.name]

            if not isinstance(value, param.type):
                # Coerce only actual numerical types, not enums
                if (
                    isinstance(value, int)
                    and issubclass(param.type, int)
                    and not issubclass(param.type, t.enum_uint8)
                ):
                    value = param.type(value)
                elif isinstance(value, bytes) and issubclass(param.type, t.ShortBytes):
                    value = param.type(value)
                else:
                    raise ValueError(
                        f"Param {param.name} is type {param.type}, got {type(value)}"
                    )

                try:
                    # XXX: Break early if a numerical type overflows
                    value.serialize()
                except Exception as e:
                    raise ValueError(
                        f"Invalid parameter value: {param.name}={value!r}"
                    ) from e

            bound_params[param.name] = (param, value)

        super().__setattr__("_bound_params", bound_params)

        if partial and all_params == given_params:
            LOGGER.warning("Partial command has no unbound parameters: %s", self)

    def to_frame(self):
        from zigpy_znp.frames import GeneralFrame

        missing_params = {p.name for p, v in self._bound_params.values() if v is None}

        if missing_params:
            raise ValueError(
                f"Cannot serialize a partial frame: missing {missing_params}"
            )

        data = b"".join(v.serialize() for p, v in self._bound_params.values())

        return GeneralFrame(self.header, data)

    @classmethod
    def from_frame(cls, frame) -> "CommandBase":
        if frame.header != cls.header:
            raise ValueError(
                f"Wrong frame header: expected {cls.header}, got {frame.header}"
            )

        data = frame.data
        params = {}

        for param in cls.schema.parameters:
            params[param.name], data = param.type.deserialize(data)

        if data:
            raise ValueError(f"Unparsed data remains at the end of the frame: {data!r}")

        return cls(**params)

    def matches(self, other: "CommandBase") -> bool:
        if type(self) is not type(other):
            return False

        assert self.header == other.header

        param_pairs = zip(self._bound_params.values(), other._bound_params.values())

        for (
            (expected_param, expected_value),
            (actual_param, actual_value),
        ) in param_pairs:
            assert expected_param == actual_param

            # Only non-None bound params are considered
            if expected_value is not None and expected_value != actual_value:
                return False

        return True

    def __eq__(self, other):
        return type(self) is type(other) and self._bound_params == other._bound_params

    def __hash__(self):
        params = tuple(self._bound_params.items())
        return hash((type(self), self.header, self.schema, params))

    def __getattr__(self, key):
        if key not in self._bound_params:
            raise AttributeError(key)

        param, value = self._bound_params[key]
        return value

    def __setattr__(self, key, value):
        raise RuntimeError("Command instances are immutable")

    def __delattr__(self, key):
        raise RuntimeError("Command instances are immutable")

    def __repr__(self):
        params = [f"{p.name}={v!r}" for p, v in self._bound_params.values()]

        return f'{self.__class__.__qualname__}({", ".join(params)})'


class DeviceState(t.enum_uint8, enum.IntEnum):
    """Indicated device state."""

    InitializedNotStarted = 0x00
    InitializedNotConnected = 0x01
    DiscoveringPANs = 0x02
    Joining = 0x03
    ReJoining = 0x04
    JoinedNotAuthenticated = 0x05
    JoinedAsEndDevice = 0x06
    JoinedAsRouter = 0x07
    StartingAsCoordinator = 0x08
    StartedAsCoordinator = 0x09
    LostParent = 0x0A


class InterPanCommand(t.uint8_t, enum.Enum):
    InterPanClr = 0x00
    InterPanSet = 0x01
    InterPanReg = 0x02
    InterPanChk = 0x03


class MTCapabilities(t.enum_uint16, enum.IntFlag):
    CAP_SYS = 0x0001
    CAP_MAC = 0x0002
    CAP_NWK = 0x0004
    CAP_AF = 0x0008
    CAP_ZDO = 0x0010
    CAP_SAPI = 0x0020
    CAP_UTIL = 0x0040
    CAP_DEBUG = 0x0080
    CAP_APP = 0x0100
    CAP_ZOAD = 0x1000


@attr.s
class Network(t.Struct):
    PanId = attr.ib(type=t.PanId, converter=t.Struct.converter(t.PanId))
    Channel = attr.ib(type=t.uint8_t, converter=t.uint8_t)
    StackProfileVersion = attr.ib(type=t.uint8_t, converter=t.uint8_t)
    BeaconOrderSuperframe = attr.ib(type=t.uint8_t, converter=t.uint8_t)
    PermitJoining = attr.ib(type=t.uint8_t, converter=t.uint8_t)


STATUS_SCHEMA = t.Schema(
    (t.Param("Status", t.Status, "Status is either Success (0) or Failure (1)"),)
)
