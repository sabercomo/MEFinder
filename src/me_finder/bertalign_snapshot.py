"""Preserve optional-backend results without needing its runtime during restore."""
from .text_alignment import _segment_set, InvalidAlignmentRequest

RUN_COLUMNS = ("alignment_run_id", "document_group_id", "pivot_source_file_id", "target_source_file_id",
               "pivot_segment_set_id", "target_segment_set_id", "algorithm", "algorithm_version",
               "parameters_json", "status", "created_at", "completed_at")
LINK_COLUMNS = ("alignment_link_id", "alignment_run_id", "order_index", "cost", "confidence", "anchor_key", "review_status")
MEMBER_COLUMNS = ("alignment_link_id", "side", "segment_id", "member_order")


def read_bertalign_result(connection, run_id):
    """Store existing IDs and links; segment-set IDs include source/segmenter hashes."""
    run = connection.execute("SELECT * FROM alignment_runs WHERE alignment_run_id = ?", (run_id,)).fetchone()
    links = connection.execute("SELECT * FROM alignment_links WHERE alignment_run_id = ? ORDER BY order_index", (run_id,)).fetchall()
    members = connection.execute("SELECT m.* FROM alignment_link_members m JOIN alignment_links l USING(alignment_link_id) WHERE l.alignment_run_id = ?", (run_id,)).fetchall()
    return {"run": [run[key] for key in RUN_COLUMNS],
            "links": [[row[key] for key in LINK_COLUMNS] for row in links],
            "members": [[row[key] for key in MEMBER_COLUMNS] for row in members]}


def restore_bertalign_result(connection, result):
    """Fail the restore transaction explicitly when anchors can no longer be retained."""
    run = dict(zip(RUN_COLUMNS, result["run"], strict=True))
    for side in ("pivot", "target"):
        current, _segments = _segment_set(connection, run[side + "_source_file_id"])
        if current != run[side + "_segment_set_id"]:
            raise InvalidAlignmentRequest("Bertalign 快照的源文本或分段版本已变化，无法保留原定位；请先导出备份并重新生成对齐。")
    for table, columns, rows in (("alignment_runs", RUN_COLUMNS, [result["run"]]),
                                  ("alignment_links", LINK_COLUMNS, result["links"]),
                                  ("alignment_link_members", MEMBER_COLUMNS, result["members"])):
        connection.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
