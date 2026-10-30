"""Dependency-aware retention of raw evidence and published dataset content."""


def sweep(store, objects, *, apply: bool = False, limit: int = 100) -> dict:
    evidence = store.expired_evidence(limit)
    snapshots = store.expired_snapshots(limit)
    reports = store.expired_reports(limit)
    datasets = store.expired_datasets(limit)
    traces = store.expired_traces(limit)
    planned = {"evidence": len(evidence), "snapshots": len(snapshots),
               "reports": len(reports), "datasets": len(datasets), "traces": len(traces),
               "applied": apply}
    if not apply:
        return planned
    return {**planned, "trials_archived": store.archive_expired_trials(),
            "evidence_purged": sum(store.purge_evidence(item["id"]) for item in evidence),
            "snapshots_purged": sum(store.purge_snapshot_object(
                item["id"], objects.delete) for item in snapshots),
            "reports_purged": sum(store.purge_report_object(
                item["id"], objects.delete) for item in reports),
            "datasets_purged": sum(store.purge_dataset_content(item["id"])
                                   for item in datasets),
            "traces_purged": sum(store.purge_trace_object(item["id"], objects.delete)
                                 for item in traces)}
