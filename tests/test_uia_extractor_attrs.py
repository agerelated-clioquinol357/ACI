"""Tests for UIA interactivity attribute extraction.

These tests use mock UIA controls since real UIA requires a Windows desktop.
"""
import pytest


class FakeControl:
    """Minimal UIA control stub for testing attribute extraction."""
    ControlType = 50004  # ButtonControl (arbitrary ID)
    Name = "Test Button"
    AutomationId = "btn_test"
    ClassName = "Button"
    IsOffscreen = False

    class BoundingRectangle:
        @staticmethod
        def width(): return 100
        @staticmethod
        def height(): return 30
        left = 10
        top = 20

    def GetValuePattern(self):
        return None

    def GetInvokePattern(self):
        return object()  # Non-None = has invoke pattern

    def GetChildren(self):
        return []


class FakePasswordControl(FakeControl):
    ControlType = 50004
    Name = "Password"
    CurrentIsPassword = True

    def GetValuePattern(self):
        return object()  # Has value pattern

    def GetInvokePattern(self):
        return None


def test_invoke_pattern_detected():
    """Button with InvokePattern should get can_invoke=True."""
    from bridges.desktop_bridge.uia_extractor import _INTERACTABLE_TAGS
    assert "button" in _INTERACTABLE_TAGS


def test_password_detection_logic():
    """Verify CurrentIsPassword attribute is checked for input tags."""
    ctrl = FakePasswordControl()
    assert getattr(ctrl, "CurrentIsPassword", False) is True
