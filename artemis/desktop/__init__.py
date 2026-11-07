"""Artemis Desktop — in-guest control agents for kind:"mac"|"win" fleet devices.

Phones are driven over adb; desktops can't be — a thin agent daemon runs on
the box and exposes the same action dialect over loopback/tailnet HTTP so the
fleet registry, console tiles and actuator seam treat it like any other
device. `macagentd` is the macOS build. Spec: voshu-docs VOSHU-DEVICE-CONTROL.
"""
