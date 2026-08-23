This fixture is a lossless gzip/base64 encoding of an untouched
`worker_client.sqlite3` produced by the released 0.9.30 storage code at
functional commit `5c94a91cd956f37776be73f872b4c34ccaccd810`.

It contains one pre-restart `c2_read` flow with two legacy waiting media
records owned by different conversation IDs. Tests decode and copy the
database before importing it through the current production storage path;
they do not rebuild the rows with current helpers.

The two JSON journals in this directory were likewise written by the same
0.9.30 production `action_journal` module and are copied unchanged by tests.
