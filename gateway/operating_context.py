"""Labels for recorded afterheat observations; never influences control."""
UNKNOWN = None
NORMAL, FROST, STARTUP, BOOST, OFF, COOLING, FAN_CHANGE, OVERRIDE, EXTERNAL = range(9)


def afterheat_context(snapshot: dict, *, pi_master: bool, uptime: float, fan_changed: bool) -> int:
    if not pi_master:
        return EXTERNAL
    if snapshot.get('standby_active') or snapshot.get('bonfire_active') or snapshot.get('afterheat_command') is None:
        return OFF
    if snapshot.get('afterheat_frost_lock') is True:
        return FROST
    if uptime < 300:
        return STARTUP
    if snapshot.get('afterheat_override_setpoint') is not None:
        return OVERRIDE
    if snapshot.get('cool_boost_active') or snapshot.get('night_cool_active') or snapshot.get('cooling_active'):
        return COOLING
    if snapshot.get('quick_boost_active'):
        return BOOST
    if fan_changed:
        return FAN_CHANGE
    return NORMAL
