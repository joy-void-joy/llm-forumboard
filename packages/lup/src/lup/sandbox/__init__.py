"""Docker-based Python sandbox, split by concern.

`models` holds the tool schemas, result types, mount topology, and error
types; `process` holds pure host helpers (output decoding, process liveness,
request deadlines); `repl` holds the container-exec REPL transport (socket
protocol and persistent namespace); `container` holds the ``Sandbox``
lifecycle (create, mount, sweep, destroy) and the post-session cleanup guard.

Importing anything here requires the ``docker`` extra
(``pip install lup[docker]``); import the concrete module you need directly.
"""
