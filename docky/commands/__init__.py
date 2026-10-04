"""Every `docky <command>`, grouped by area. docky/cli.py dispatches to these."""
from .status import cmd_status, cmd_projects, cmd_urls, cmd_top
from .updates import cmd_updates, cmd_rollback
from .cleanup import cmd_sweep, cmd_orphans
from .lifecycle import cmd_lifecycle
from .remove import cmd_remove

__all__ = ['cmd_status', 'cmd_projects', 'cmd_urls', 'cmd_top', 'cmd_updates', 'cmd_rollback', 'cmd_sweep', 'cmd_orphans', 'cmd_lifecycle', 'cmd_remove']
