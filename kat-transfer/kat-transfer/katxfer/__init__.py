"""KAT transfer service.

A stand-alone process that drains new rows out of the local KAT SQLite
database and pushes them to the remote archive, driven by ZeroMQ
notifications with a periodic sweep as a safety net.
"""

__version__ = "0.1.0"
