# KDE Wallpaper Picker

Selector visual de fondos de pantalla para **KDE Plasma 6**, lanzado con atajo de teclado y con tres modos de visualización: cuadrícula, carrusel de doble cinta y panal hexagonal.

## Características

- **Fondo transparente**: la app se superpone sobre el escritorio sin tapar el contenido.
- **Tres vistas**:
  - `▦ Cuadrícula` – cuadrícula con tarjetas y miniaturas.
  - `⇄ Carrusel` – dos cintas infinitas (arriba hacia la izquierda, abajo hacia la derecha), pausas al pasar el ratón.
  - `⬡ Hexágono` – panal denso con hexágonos regulares.
- **Solo imágenes**: no se muestra el nombre de los archivos en la interfaz.
- **Teclado completo**: flechas para mover, `Enter` para aplicar, `Esc` para cerrar.
- **Cambio de vista con `Shift`** (ciclo) o con `1`, `2`, `3`.
- **Instancia única** a través de `QLocalServer`.
- **Miniaturas cacheadas** en `~/.cache/wallpaper-picker/`.
- **Persistencia** del último modo de vista en `~/.config/wallpaper-picker.conf`.
- **Notificaciones** con `notify-send` al cambiar el fondo.

## Instalación

```bash
cp wallpaper-picker.py ~/.local/bin/wallpaper-picker
chmod +x ~/.local/bin/wallpaper-picker
cp wallpaper-picker.desktop ~/.local/share/applications/
```

Luego, en **Configuración del sistema > Atajos de teclado > Atajos globales**, busca `wallpaper-picker` y asigna **Meta+Shift+W** (Meta+W ya suele estar ocupado por el Overview).

## Uso

| Tecla | Acción |
| --- | --- |
| `Meta+Shift+W` | Abrir el selector |
| `←` `→` `↑` `↓` | Mover la selección |
| `Enter` | Aplicar el fondo seleccionado |
| `Esc` | Cerrar |
| `Shift` | Cambiar de vista (ciclo) |
| `1` `2` `3` | Cuadrícula / Carrusel / Hexágono |
| `V` | Cambiar de vista (ciclo) |
| `Ctrl+R` | Recargar la carpeta de wallpapers |

## Configuración

La carpeta de origen se elige con la variable de entorno `WALLPAPER_PICKER_DIR` o, por defecto, `~/Imágenes/Wallpapers`.

## Requisitos

- Python 3.11+
- PySide6
- KDE Plasma 6
- `plasma-apply-wallpaperimage`
- `notify-send` (libnotify)