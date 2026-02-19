"""Collector package for KubeRCA.

Provides Kubernetes watch-stream collectors that ingest cluster events and
resource changes into the KubeRCA analysis pipeline.

Submodules
----------
watcher      -- BaseWatcher: reconnect logic, exponential back-off, relist recovery.
event_watcher -- EventWatcher: cluster-wide v1.Event ingestion with 2 h buffer.
pod_watcher   -- PodWatcher: pod phase transitions and spec-change snapshots.
node_watcher  -- NodeWatcher: node condition monitoring (Ready, pressure conditions).
"""
