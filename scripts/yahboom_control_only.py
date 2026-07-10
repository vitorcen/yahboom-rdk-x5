#!/usr/bin/env python3
"""Run Yahboom's original control APP without opening the MIPI camera."""

import os
import runpy
import sys
import time


APP_DIR = "/home/sunrise/sunriseRobot/app_SunriseRobot"
APP_MAIN = os.path.join(APP_DIR, "app_SunriseRobot.py")


class DisabledMipiCamera:
    """Camera-compatible stub that leaves CSI0 available to TogetheROS."""

    def __init__(self, *args, **kwargs):
        pass

    def isOpened(self):
        return False

    def get_frame(self):
        time.sleep(0.1)
        return False, bytes({1})

    def get_frame_jpg(self, *args, **kwargs):
        return self.get_frame()

    def read(self):
        return self.get_frame()

    def release(self):
        pass


def main():
    os.chdir(APP_DIR)
    sys.path.insert(0, APP_DIR)

    import SunriseRobotLib

    SunriseRobotLib.Mipi_Camera = DisabledMipiCamera
    runpy.run_path(APP_MAIN, run_name="__main__")


if __name__ == "__main__":
    main()
