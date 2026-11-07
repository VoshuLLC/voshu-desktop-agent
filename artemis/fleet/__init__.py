"""Artemis Device Fleet — multi-device registry, cloning and streaming.

Voshu phone-farm layer: one golden AVD, N clones, every device an
ADB-over-TCP endpoint Artemis can drive. Registry is JSON-persisted;
streams are per-serial screencap MJPEG (same technique as
device_stream_service, but keyed by serial so every tile gets its own).
"""
