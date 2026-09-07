"""Meter backend package.

Per-feature AI cost attribution: what each feature cost to *build* (AI coding
tools) and to *run* (inference). The connector path is the must-ship core;
the metering hook (M7) is a precision tier layered on top.

Submodules are added per build-plan milestone (M1+). M0 is the skeleton.
"""

__version__ = "0.1.0"
