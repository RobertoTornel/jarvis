"""Builtin tools module.

This module contains all the built-in tools available to the Claire system.
Each tool is implemented using the common Tool interface for consistency.
"""

# Import all tool classes
from .screenshot import ScreenshotTool
from .web_search import WebSearchTool
from .local_files import LocalFilesTool
from .fetch_web_page import FetchWebPageTool
from .nutrition.log_meal import LogMealTool
from .nutrition.fetch_meals import FetchMealsTool
from .nutrition.delete_meal import DeleteMealTool
from .time_tool import TimeTool
from .stop import StopTool
from .schedule import ScheduleTool
from .timer import TimerTool
from .senses import BrowseWebTool, LookThroughCameraTool, ReadScreenTool

# Import supporting functions that may still be used elsewhere

__all__ = [
    # Tool classes
    'ScreenshotTool',
    'WebSearchTool',
    'LocalFilesTool',
    'FetchWebPageTool',
    'LogMealTool',
    'FetchMealsTool',
    'DeleteMealTool',
    'TimeTool',
    'StopTool',
    'ScheduleTool',
    'TimerTool',
    'ReadScreenTool',
    'LookThroughCameraTool',
    'BrowseWebTool',
]
