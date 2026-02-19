"""Application bootstrap for KubeRCA.

Wires all components in dependency order and manages the asyncio lifecycle.
Startup order: config → logging → K8s client → cache → ledger → collectors
              → rules → LLM → coordinator → queue → scout → notifications
              → MCP → REST

Shutdown is fully graceful: components are stopped in reverse startup order.
Each component's start/stop error is caught and logged independently so that
a single component failure does not prevent the rest from shutting down cleanly.
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Any

from kuberca.config import load_config
from kuberca.models.config import KubeRCAConfig
from kuberca.observability.logging import get_logger, setup_logging

if TYPE_CHECKING:
    import structlog

_SHUTDOWN_GRACE_SECONDS = 15


class _ComponentError(Exception):
    """Raised when a mandatory component fails to start."""

    def __init__(self, component: str, cause: Exception) -> None:
        super().__init__(f"Component '{component}' failed to start: {cause}")
        self.component = component
        self.cause = cause


class KubeRCAApp:
    """Application root.  Owns every component and coordinates their lifecycle.

    Each public method is idempotent with respect to repeated calls: calling
    ``stop()`` on an app that was never started (or already stopped) is safe.
    """

    def __init__(self) -> None:
        self.config: KubeRCAConfig | None = None

        # Component handles — typed as object so we can reference them without
        # importing the (not-yet-implemented) concrete types at module level.
        # The start() method populates every slot before moving to the next.
        self._k8s_client: object | None = None
        self._cache: object | None = None
        self._ledger: object | None = None
        self._collector: object | None = None
        self._rule_engine: object | None = None
        self._llm_analyzer: object | None = None
        self._coordinator: object | None = None
        self._analysis_queue: object | None = None
        self._scout: object | None = None
        self._notifications: object | None = None
        self._mcp_server: object | None = None
        self._rest_server: object | None = None
        self._dependency_graph: object | None = None

        # Background tasks that must be cancelled on shutdown
        self._background_tasks: list[asyncio.Task[None]] = []

        self._running = False
        self._log: structlog.stdlib.BoundLogger | None = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all components in dependency order.

        Raises _ComponentError if a mandatory component cannot start.
        The caller (main()) re-raises this as a non-zero exit.
        """
        # --- 1. Configuration -------------------------------------------
        self.config = load_config()

        # --- 2. Logging -------------------------------------------------
        setup_logging(self.config.log.level)
        self._log = get_logger("app")
        self._log.info("kuberca starting", version=_kuberca_version())

        # --- 3. Kubernetes client ----------------------------------------
        await self._start_k8s_client()

        # --- 4. Resource cache -------------------------------------------
        await self._start_cache()

        # --- 4b. Dependency graph ----------------------------------------
        await self._start_dependency_graph()

        # --- 5. Change ledger --------------------------------------------
        await self._start_ledger()

        # --- 6. Event collectors -----------------------------------------
        await self._start_collector()

        # --- 7. Rule engine ----------------------------------------------
        await self._start_rule_engine()

        # --- 8. LLM analyzer (optional) ----------------------------------
        await self._start_llm()

        # --- 9. Analyst coordinator --------------------------------------
        await self._start_coordinator()

        # --- 10. Analysis queue ------------------------------------------
        await self._start_analysis_queue()

        # --- 11. Scout anomaly detector ----------------------------------
        await self._start_scout()

        # --- 12. Notification dispatcher ---------------------------------
        await self._start_notifications()

        # --- 13. MCP server ----------------------------------------------
        await self._start_mcp()

        # --- 14. REST API ------------------------------------------------
        await self._start_rest()

        # --- 15. Rolling rate gauge updater --------------------------------
        await self._start_rate_gauge_updater()

        self._running = True
        self._log.info("kuberca started", port=self.config.api.port)

    # ------------------------------------------------------------------
    # Component startup helpers
    # ------------------------------------------------------------------

    async def _start_k8s_client(self) -> None:
        """Initialise the kubernetes-asyncio client from in-cluster config or kubeconfig."""
        assert self._log is not None
        assert self.config is not None
        self._log.debug("starting k8s client")
        try:
            # Import lazily — this module pulls in kubernetes-asyncio which
            # attempts cluster auto-detection on import in some versions.
            import kubernetes_asyncio.config as k8s_config  # type: ignore[import-untyped]

            try:
                # load_incluster_config() is synchronous in kubernetes-asyncio
                k8s_config.load_incluster_config()
                self._log.info("k8s client configured from in-cluster service account")
            except k8s_config.ConfigException:
                # load_kube_config() is async in kubernetes-asyncio
                await k8s_config.load_kube_config()
                self._log.info("k8s client configured from kubeconfig")

            # Store sentinel so stop() knows the client was initialised
            self._k8s_client = True
        except Exception as exc:
            raise _ComponentError("k8s_client", exc) from exc

    async def _start_cache(self) -> None:
        """Initialise ResourceCache and populate via initial list calls."""
        assert self._log is not None
        assert self.config is not None
        self._log.debug("starting resource cache")
        try:
            from kubernetes_asyncio import client as k8s_client  # type: ignore[import-untyped]

            from kuberca.cache import ResourceCache  # type: ignore[attr-defined]

            cache = ResourceCache()
            v1 = k8s_client.CoreV1Api()
            apps_v1 = k8s_client.AppsV1Api()
            batch_v1 = k8s_client.BatchV1Api()

            api_map = {
                "Pod": v1,
                "Node": v1,
                "Event": v1,
                "Namespace": v1,
                "Service": v1,
                "Endpoints": v1,
                "ConfigMap": v1,
                "Secret": v1,
                "PersistentVolumeClaim": v1,
                "PersistentVolume": v1,
                "ResourceQuota": v1,
                "ServiceAccount": v1,
                "Deployment": apps_v1,
                "ReplicaSet": apps_v1,
                "StatefulSet": apps_v1,
                "DaemonSet": apps_v1,
                "Job": batch_v1,
                "CronJob": batch_v1,
            }
            await cache.populate(api_map)
            self._cache = cache
            self._log.info("resource cache started")
        except Exception as exc:
            raise _ComponentError("cache", exc) from exc

    async def _start_dependency_graph(self) -> None:
        """Build the in-memory dependency graph and seed it from the cache.

        Non-fatal: if the graph fails to build the app continues without
        blast-radius / state-context enrichment.
        """
        assert self._log is not None
        assert self._cache is not None
        self._log.debug("starting dependency graph")
        try:
            from kuberca.graph import DependencyGraph
            from kuberca.models.resources import ResourceSnapshot

            graph = DependencyGraph()
            self._seed_graph_from_cache(graph, ResourceSnapshot)
            self._dependency_graph = graph
            self._log.info(
                "dependency graph started",
                nodes=graph.node_count,
                edges=graph.edge_count,
            )
        except Exception as exc:
            self._log.warning(
                "dependency graph failed to start; blast-radius disabled",
                error=str(exc),
            )
            self._dependency_graph = None

    def _seed_graph_from_cache(
        self,
        graph: object,
        resource_snapshot_cls: type,
    ) -> None:
        """Iterate the cache store and add every entry to the graph."""
        from datetime import UTC, datetime

        cache = self._cache
        store = cache._store  # type: ignore[union-attr]
        for kind, ns_map in store.items():
            for ns, name_map in ns_map.items():
                for name, cached in name_map.items():
                    raw_obj = {
                        "metadata": {
                            "namespace": cached.namespace,
                            "name": cached.name,
                            "labels": cached.labels,
                            "annotations": cached.annotations,
                            "resourceVersion": cached.resource_version,
                        },
                        "spec": cached.spec,
                        "status": cached.status,
                    }
                    snapshot = resource_snapshot_cls(
                        kind=cached.kind,
                        namespace=cached.namespace,
                        name=cached.name,
                        spec_hash="",
                        spec=raw_obj,
                        captured_at=datetime.now(tz=UTC),
                        resource_version=cached.resource_version,
                    )
                    graph.add_resource(snapshot)  # type: ignore[union-attr]

    async def _start_ledger(self) -> None:
        """Initialise ChangeLedger (optionally backed by SQLite)."""
        assert self._log is not None
        assert self.config is not None
        self._log.debug("starting change ledger")
        try:
            from kuberca.ledger.change_ledger import ChangeLedger

            # Parse retention string (e.g. "6h") to integer hours
            retention_str = self.config.change_ledger.retention
            retention_hours = int(retention_str.rstrip("mhd"))
            if retention_str.endswith("d"):
                retention_hours *= 24

            ledger = ChangeLedger(
                max_versions=self.config.change_ledger.max_versions,
                retention_hours=retention_hours,
            )
            self._ledger = ledger
            self._log.info(
                "change ledger started",
                persistence=self.config.change_ledger.persistence_enabled,
            )
        except Exception as exc:
            raise _ComponentError("ledger", exc) from exc

    async def _start_collector(self) -> None:
        """Start the event collector watch loops."""
        assert self._log is not None
        assert self._cache is not None
        assert self._ledger is not None
        assert self.config is not None
        self._log.debug("starting event collector")
        try:
            from kubernetes_asyncio import client as k8s_client  # type: ignore[import-untyped]

            from kuberca.collector.event_watcher import EventWatcher
            from kuberca.collector.node_watcher import NodeWatcher
            from kuberca.collector.pod_watcher import PodWatcher

            v1 = k8s_client.CoreV1Api()
            cluster_id = self.config.cluster_id or ""

            event_watcher = EventWatcher(v1, cluster_id=cluster_id)
            pod_watcher = PodWatcher(v1, cluster_id=cluster_id)
            node_watcher = NodeWatcher(v1, cluster_id=cluster_id)

            # Wire pod and node watchers to keep the ResourceCache updated
            # as new resources are created/modified/deleted after startup.
            cache = self._cache
            graph = self._dependency_graph
            _original_pod_handle = pod_watcher._handle_event

            async def _pod_handle_with_cache(event_type: str, obj: Any, raw: dict[str, Any]) -> None:
                metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
                ns = str(metadata.get("namespace", ""))
                name = str(metadata.get("name", ""))
                if name:
                    if event_type in ("ADDED", "MODIFIED"):
                        cache.update("Pod", ns, name, raw)
                        _feed_graph(graph, "Pod", ns, name, raw)
                    elif event_type == "DELETED":
                        cache.remove("Pod", ns, name)
                        if graph is not None:
                            graph.remove_resource("Pod", ns, name)  # type: ignore[union-attr]
                await _original_pod_handle(event_type, obj, raw)

            pod_watcher._handle_event = _pod_handle_with_cache  # type: ignore[assignment]

            _original_node_handle = node_watcher._handle_event

            async def _node_handle_with_cache(event_type: str, obj: Any, raw: dict[str, Any]) -> None:
                metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
                name = str(metadata.get("name", ""))
                if name:
                    if event_type in ("ADDED", "MODIFIED"):
                        cache.update("Node", "", name, raw)
                        _feed_graph(graph, "Node", "", name, raw)
                    elif event_type == "DELETED":
                        cache.remove("Node", "", name)
                        if graph is not None:
                            graph.remove_resource("Node", "", name)  # type: ignore[union-attr]
                await _original_node_handle(event_type, obj, raw)

            node_watcher._handle_event = _node_handle_with_cache  # type: ignore[assignment]

            # Start all watchers as background tasks
            for watcher in [event_watcher, pod_watcher, node_watcher]:
                await watcher.start()

            # Store the event watcher as the primary collector reference
            self._collector = event_watcher
            self._log.info("event collectors started (event, pod, node)")
        except Exception as exc:
            raise _ComponentError("collector", exc) from exc

    async def _start_rule_engine(self) -> None:
        """Build the rule engine and register all rules."""
        assert self._log is not None
        assert self.config is not None
        assert self._cache is not None
        assert self._ledger is not None
        self._log.debug("starting rule engine")
        try:
            from kuberca.rules import build_rule_engine  # type: ignore[attr-defined]

            engine = build_rule_engine(
                cache=self._cache,
                ledger=self._ledger,
                tier2_enabled=self.config.rule_engine.tier2_enabled,
            )
            self._rule_engine = engine
            self._log.info("rule engine started")
        except Exception as exc:
            raise _ComponentError("rule_engine", exc) from exc

    async def _start_llm(self) -> None:
        """Initialise the Ollama LLM analyzer if enabled."""
        assert self._log is not None
        assert self.config is not None
        if not self.config.ollama.enabled:
            self._log.info("llm analyzer disabled (ollama.enabled=false)")
            self._llm_analyzer = None
            return

        self._log.debug("starting llm analyzer")
        try:
            from kuberca.llm.analyzer import LLMAnalyzer

            analyzer = LLMAnalyzer(config=self.config.ollama)
            health_ok = await analyzer.health_check()
            self._llm_analyzer = analyzer
            self._log.info("llm analyzer started", model=self.config.ollama.model, healthy=health_ok)
        except Exception as exc:
            # LLM is optional — log and continue in rule-engine-only mode
            self._log.warning(
                "llm analyzer failed to start; running in rule-engine-only mode",
                error=str(exc),
            )
            self._llm_analyzer = None

    async def _start_coordinator(self) -> None:
        """Wire the analyst coordinator."""
        assert self._log is not None
        assert self._rule_engine is not None
        assert self._cache is not None
        assert self._ledger is not None
        self._log.debug("starting analyst coordinator")
        try:
            from kuberca.analyst import AnalystCoordinator  # type: ignore[attr-defined]

            coordinator = AnalystCoordinator(
                rule_engine=self._rule_engine,
                llm_analyzer=self._llm_analyzer,
                cache=self._cache,
                ledger=self._ledger,
                event_buffer=self._collector,
                config=self.config,
                dependency_graph=self._dependency_graph,
            )
            self._coordinator = coordinator
            self._log.info("analyst coordinator started")
        except Exception as exc:
            raise _ComponentError("coordinator", exc) from exc

    async def _start_analysis_queue(self) -> None:
        """Start the bounded analysis request queue."""
        assert self._log is not None
        assert self._coordinator is not None
        self._log.debug("starting analysis queue")
        try:
            from kuberca.analyst.queue import WorkQueue

            # WorkQueue takes an analyze_fn: (resource, time_window) -> result
            coordinator = self._coordinator
            queue = WorkQueue(
                analyze_fn=coordinator.analyze,  # type: ignore[union-attr]
            )
            task = asyncio.create_task(queue.start(), name="analysis-queue")
            self._background_tasks.append(task)
            self._analysis_queue = queue
            self._log.info("analysis queue started")
        except Exception as exc:
            raise _ComponentError("analysis_queue", exc) from exc

    async def _start_scout(self) -> None:
        """Start the anomaly scout."""
        assert self._log is not None
        assert self.config is not None
        self._log.debug("starting scout")
        try:
            from kuberca.scout.detector import AnomalyDetector

            scout = AnomalyDetector(
                cooldown=self.config.scout.anomaly_cooldown,
            )
            self._scout = scout
            self._log.info("scout started")
        except Exception as exc:
            raise _ComponentError("scout", exc) from exc

    async def _start_notifications(self) -> None:
        """Configure notification dispatcher channels."""
        assert self._log is not None
        assert self.config is not None
        assert self._scout is not None
        self._log.debug("starting notifications")
        try:
            from kuberca.notifications import build_notification_dispatcher  # type: ignore[attr-defined]

            dispatcher = build_notification_dispatcher(config=self.config.notifications)
            self._notifications = dispatcher
            self._log.info("notifications started")
        except Exception as exc:
            # Notification failure is non-fatal: alerts won't fire but analysis works
            self._log.warning(
                "notification dispatcher failed to start; alerts will be suppressed",
                error=str(exc),
            )
            self._notifications = None

    async def _start_mcp(self) -> None:
        """Start the MCP stdio server."""
        assert self._log is not None
        assert self._coordinator is not None
        assert self._cache is not None
        self._log.debug("starting mcp server")
        try:
            from kuberca.mcp import MCPServer  # type: ignore[attr-defined]

            mcp = MCPServer(coordinator=self._coordinator, cache=self._cache)
            task = asyncio.create_task(mcp.start(), name="mcp-server")
            self._background_tasks.append(task)
            self._mcp_server = mcp
            self._log.info("mcp server started")
        except Exception as exc:
            # MCP is non-fatal; REST API remains available
            self._log.warning(
                "mcp server failed to start; stdio interface unavailable",
                error=str(exc),
            )
            self._mcp_server = None

    async def _start_rest(self) -> None:
        """Start the uvicorn REST server."""
        assert self._log is not None
        assert self.config is not None
        assert self._coordinator is not None
        assert self._cache is not None
        self._log.debug("starting rest api")
        try:
            import uvicorn  # type: ignore[import-untyped]

            from kuberca.api import build_app  # type: ignore[attr-defined]

            fastapi_app = build_app(
                coordinator=self._coordinator,
                cache=self._cache,
                rule_engine=self._rule_engine,
                llm_analyzer=self._llm_analyzer,
                config=self.config,
                analysis_queue=self._analysis_queue,
            )
            uv_config = uvicorn.Config(
                app=fastapi_app,
                host="0.0.0.0",
                port=self.config.api.port,
                log_config=None,  # structlog handles all logging
                access_log=False,
            )
            server = uvicorn.Server(uv_config)
            task = asyncio.create_task(server.serve(), name="rest-server")
            self._background_tasks.append(task)
            self._rest_server = server
            self._log.info("rest api started", port=self.config.api.port)
        except Exception as exc:
            raise _ComponentError("rest", exc) from exc

    async def _start_rate_gauge_updater(self) -> None:
        """Launch a periodic task that recomputes rolling rate gauges every 60s."""
        assert self._log is not None

        async def _updater() -> None:
            from kuberca.observability.metrics import compute_rolling_rates

            while True:
                try:
                    compute_rolling_rates()
                except Exception as exc:
                    if self._log:
                        self._log.debug("rate_gauge_update_error", error=str(exc))
                await asyncio.sleep(60)

        task = asyncio.create_task(_updater(), name="rate-gauge-updater")
        self._background_tasks.append(task)
        self._log.info("rate gauge updater started")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Gracefully stop all components in reverse startup order.

        Each component's stop is wrapped independently; a failure in one
        component's teardown does not prevent the others from stopping.
        """
        if not self._running and self._log is None:
            # Never started — nothing to do
            return

        log = self._log or get_logger("app")
        log.info("kuberca shutting down")

        self._running = False

        # Cancel background tasks first so watch loops stop producing events
        # before we tear down the consumers.
        for task in reversed(self._background_tasks):
            if not task.done():
                task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Stop components in reverse initialisation order
        await self._stop_component("rest", self._rest_server)
        await self._stop_component("mcp", self._mcp_server)
        await self._stop_component("notifications", self._notifications)
        await self._stop_component("scout", self._scout)
        await self._stop_component("analysis_queue", self._analysis_queue)
        await self._stop_component("coordinator", self._coordinator)
        await self._stop_component("llm", self._llm_analyzer)
        await self._stop_component("rule_engine", self._rule_engine)
        await self._stop_component("collector", self._collector)
        await self._stop_component("ledger", self._ledger)
        await self._stop_component("cache", self._cache)
        self._dependency_graph = None
        await self._stop_k8s_client()

        log.info("kuberca stopped")

    async def _stop_component(self, name: str, component: object | None) -> None:
        """Call stop() on a component if it has that method, catching all errors."""
        if component is None:
            return
        log = self._log or get_logger("app")
        stop_fn = getattr(component, "stop", None)
        if stop_fn is None:
            return
        try:
            result = stop_fn()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=_SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            log.warning("component stop timed out", component=name, timeout=_SHUTDOWN_GRACE_SECONDS)
        except Exception as exc:
            log.error("component stop raised an error", component=name, error=str(exc))

    async def _stop_k8s_client(self) -> None:
        """Close the kubernetes-asyncio ApiClient connection pool."""
        if self._k8s_client is None:
            return
        log = self._log or get_logger("app")
        try:
            # kubernetes-asyncio uses a module-level ApiClient; close it cleanly.
            from kubernetes_asyncio import client as k8s_client  # type: ignore[import-untyped]

            api_client = k8s_client.ApiClient()
            await api_client.close()
        except Exception as exc:
            log.debug("k8s client close raised (non-fatal)", error=str(exc))


def _feed_graph(
    graph: object | None,
    kind: str,
    namespace: str,
    name: str,
    raw_obj: dict[str, Any],
) -> None:
    """Build a ResourceSnapshot from a raw watch event and feed it to the graph."""
    if graph is None:
        return
    try:
        from datetime import UTC, datetime

        from kuberca.models.resources import ResourceSnapshot

        snapshot = ResourceSnapshot(
            kind=kind,
            namespace=namespace,
            name=name,
            spec_hash="",
            spec=raw_obj,
            captured_at=datetime.now(tz=UTC),
            resource_version=str(raw_obj.get("metadata", {}).get("resourceVersion", "")),
        )
        graph.add_resource(snapshot)  # type: ignore[union-attr]
    except Exception:
        pass  # Graph update failure is non-fatal


def _kuberca_version() -> str:
    from kuberca import __version__

    return __version__


# ---------------------------------------------------------------------------
# Async entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    """Create the app, register OS signals, run until shutdown is requested."""
    app = KubeRCAApp()
    loop = asyncio.get_running_loop()

    shutdown_triggered = False

    def _request_shutdown() -> None:
        nonlocal shutdown_triggered
        if shutdown_triggered:
            return
        shutdown_triggered = True
        asyncio.create_task(app.stop(), name="shutdown")

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)

    try:
        await app.start()
        # Block until shutdown is triggered (background tasks run concurrently)
        while app._running:
            await asyncio.sleep(1)
    except _ComponentError as exc:
        # A mandatory component failed; log and exit non-zero
        log = get_logger("app")
        log.critical(
            "fatal startup error",
            component=exc.component,
            error=str(exc.cause),
        )
        await app.stop()
        raise SystemExit(1) from exc
    finally:
        # Ensure stop runs even if start raises or is interrupted
        if app._running:
            await app.stop()
