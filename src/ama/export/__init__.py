"""
Export sinks for migration plan artifacts (Jira, Confluence).
"""

from ama.export.config import ExportConfig
from ama.export.confluence_sink import ConfluenceExportSink
from ama.export.jira_sink import JiraExportSink
from ama.export.writer import write_export

__all__ = [
    "ConfluenceExportSink",
    "ExportConfig",
    "JiraExportSink",
    "write_export",
]
