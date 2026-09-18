"""测试夹具：搭建"三代同堂"家庭。"""

import tempfile
import os

from home import (
    Device,
    DeviceCapability,
    HomeOrchestrator,
    LocalStore,
    Member,
    MemberRole,
    PrivacyCategory,
    RuleAction,
    TimeWindow,
)

NIGHT = TimeWindow(22, 6)
TS = 1_700_000_000.0  # 固定的夜间时间基准（对应 hour 由事件显式给出）


def build_home(persistent_path=None):
    """构建一个已登记成员、设备、同意与规则的完整家庭。"""
    if persistent_path is None:
        persistent_path = os.path.join(tempfile.mkdtemp(), "home.json")
    store = LocalStore(persistent_path)
    home = HomeOrchestrator(store)

    elder = Member("m_elder", "奶奶", MemberRole.ELDER,
                   frozenset({DeviceCapability.LIGHT, DeviceCapability.SLEEP_MONITOR}))
    dad = Member("m_dad", "爸爸", MemberRole.PARENT, frozenset(), is_admin=True)
    mom = Member("m_mom", "妈妈", MemberRole.PARENT, frozenset(), is_admin=True)
    kid = Member("m_kid", "孩子", MemberRole.CHILD, frozenset())
    for member in (elder, dad, mom, kid):
        home.register_member(member)

    lock = Device("d_lock", "大门门锁", frozenset({DeviceCapability.LOCK}))
    light = Device("d_light", "奶奶卧室夜灯", frozenset({DeviceCapability.LIGHT}),
                   requires_sensors=frozenset({"presence"}), safety_critical=True)
    cam = Device("d_cam", "客厅摄像头",
                 frozenset({DeviceCapability.CAMERA_CAPTURE,
                            DeviceCapability.CAMERA_STREAM}),
                 privacy_category=PrivacyCategory.IMAGE)
    ac = Device("d_ac", "卧室空调",
                frozenset({DeviceCapability.CLIMATE, DeviceCapability.POWER}),
                requires_sensors=frozenset({"temp"}))
    sleep = Device("d_sleep", "睡眠监测带",
                   frozenset({DeviceCapability.SLEEP_MONITOR}),
                   privacy_category=PrivacyCategory.HEALTH)
    for device in (lock, light, cam, ac, sleep):
        home.register_device(device)
    home.devices.set_sensors("d_light", frozenset({"presence"}))
    home.devices.set_sensors("d_ac", frozenset({"temp"}))

    # 奶奶就摄像/健康数据给出的同意
    home.gate.grant("m_elder", PrivacyCategory.IMAGE, "night_safety")
    home.gate.grant("m_elder", PrivacyCategory.IMAGE, "guest_verification")
    home.gate.grant("m_elder", PrivacyCategory.HEALTH, "health_monitoring")

    devices = home.device_map()
    # 老人设定的夜间照明（安全优先）
    home.rules.create("奶奶夜间照明", "m_elder", "night_window",
                      [RuleAction("d_light", DeviceCapability.LIGHT, "on")],
                      devices, window=NIGHT, safety_level=1)
    # 父母的省电关灯（与老人照明竞争，安全级别更低）
    home.rules.create("父母睡前关灯省电", "m_dad", "night_window",
                      [RuleAction("d_light", DeviceCapability.LIGHT, "off")],
                      devices, window=NIGHT, safety_level=0)
    # 父母对摄像画面的夜间限制（最高优先级的隐私保护）
    home.rules.create("夜间停止查看摄像", "m_dad", "night_window",
                      [RuleAction("d_cam", DeviceCapability.CAMERA_STREAM, "stop")],
                      devices, window=NIGHT, purpose="night_safety", safety_level=4)
    # 夜间自动上锁（人身安全）：常驻策略，夜间任何时刻都与开锁意图正面竞争
    home.rules.create("夜间自动上锁", "m_dad", "*",
                      [RuleAction("d_lock", DeviceCapability.LOCK, "lock")],
                      devices, window=NIGHT, safety_level=2)

    return home, store, {
        "elder": elder, "dad": dad, "mom": mom, "kid": kid,
        "lock": lock, "light": light, "cam": cam, "ac": ac, "sleep": sleep,
    }
