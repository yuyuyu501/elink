import pyglet
import numpy as np
from pyglet.window import key

# Create a test image: green with text
data = np.zeros((200, 400, 3), dtype=np.uint8)
data[:, :, 1] = 255  # Green (BGR)
data[50:150, 50:350] = [0, 0, 255]  # Blue rectangle

window = pyglet.window.Window(640, 480, 'Sprite Test', resizable=True)
image_data = pyglet.image.ImageData(400, 200, 'BGR', data.tobytes())
texture = image_data.get_texture()
sprite = pyglet.sprite.Sprite(texture)

@window.event
def on_draw():
    window.clear()
    sprite.draw()

@window.event
def on_resize(w, h):
    from pyglet.math import Mat4
    window.viewport = (0, 0, w, h)
    window.projection = Mat4.orthogonal_projection(0, w, 0, h, -1, 1)

@window.event
def on_key_press(symbol, mods):
    if symbol == key.ESCAPE:
        pyglet.app.exit()

pyglet.app.run()
