"""An isolated, owned maintenance browser; independent from human windows."""

from contextlib import contextmanager

from signin_window.automation.runtime import open_browser, virtual_display


class PatchrightRuntime:
    @contextmanager
    def open(self, profile, *, require_virtual_display=True, launch_config=None):
        options = {} if launch_config is None else launch_config.model_dump()
        with virtual_display() as display, open_browser(profile, display=display, **options) as browser:
            yield browser

    def close(self):
        # Every owned browser is closed by its context before returning.
        pass
