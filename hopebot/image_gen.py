"""
Draw flow-field generative art

Original author: The Absolute Tinkerer
Modified by mal
GPL-3.0 license
https://github.com/Absolute-Tinkerer/Generative-Art/blob/8b9423a644f1b794396848df381bf941bd0c250d/examples.py
"""

from __future__ import annotations

from asyncio import Lock
import math
from os import environ
from random import randint

import numpy
from perlin_numpy import generate_perlin_noise_2d
from PySide6.QtCore import QBuffer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication


environ["QT_QPA_PLATFORM"] = "offscreen"
environ["XDG_RUNTIME_DIR"] = "/tmp/runtime-hopebot"
lock = Lock()


async def draw_flow_field(
    width: int,
    height: int,
    seed: int = randint(0, 100000000),
    num: int = 1,
    line_alpha: int = 25,
) -> QPixmap:
    numpy.random.seed(seed)
    mod = numpy.random.randint(0, 255)  # Colors

    await lock.acquire()
    app = QApplication()
    image = QPixmap(width, height)
    p = QPainter()
    p.begin(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    hue = numpy.random.randint(0, 255)
    val = numpy.random.randint(0, 64)
    p.fillRect(0, 0, width, height, QColor.fromHsv(hue, 255, val, 255))
    p.setPen(QPen(QColor(150, 150, 225, 5), 2))

    for j in range(num):
        p_noise = generate_perlin_noise_2d((width, height), (2, 2))

        MAX_LENGTH = 2 * width
        STEP_SIZE = 0.001 * max(width, height)
        NUM = int(width * height / 1000)
        POINTS = [
            (numpy.random.randint(0, width - 1), numpy.random.randint(0, height - 1))
            for i in range(NUM)
        ]

        for k, (x_s, y_s) in enumerate(POINTS):
            c_len = 0

            # Actually draw the flow field
            while c_len < MAX_LENGTH:
                # Set the pen color for this segment
                sat = 200 * (MAX_LENGTH - c_len) / MAX_LENGTH
                hue = (mod + 130 * (height - y_s) / height) % 360
                p.setPen(QPen(QColor.fromHsv(int(hue), int(sat), 255, line_alpha), 2))

                # angle between -pi and pi
                angle = p_noise[int(x_s), int(y_s)] * numpy.pi

                # Compute the new point
                x_f = x_s + STEP_SIZE * math.cos(angle)
                y_f = y_s + STEP_SIZE * math.sin(angle)

                # Draw the line
                p.drawLine(QPointF(x_s, y_s), QPointF(x_f, y_f))

                # Update the line length
                c_len += math.sqrt((x_f - x_s) ** 2 + (y_f - y_s) ** 2)

                # Break from the loop if the new point is outside our image bounds
                # or if we've exceeded the line length; otherwise update the point
                if (
                    x_f < 0
                    or x_f >= width
                    or y_f < 0
                    or y_f >= height
                    or c_len > MAX_LENGTH
                ):
                    break
                else:
                    x_s, y_s = x_f, y_f
    p.end()
    buffer = QBuffer()
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "jpg", 80)
    data = buffer.data().data()
    app.shutdown()
    lock.release()
    return data
