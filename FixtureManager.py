"""Fixture Manager add-in for Autodesk Fusion."""

import traceback

import adsk.core

from .fm import commands


def run(context):
    try:
        commands.start()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Fixture Manager failed to start:\n' + traceback.format_exc())


def stop(context):
    try:
        commands.stop()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Fixture Manager failed to stop cleanly:\n' + traceback.format_exc())
