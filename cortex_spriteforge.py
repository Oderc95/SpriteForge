import bpy
import sys
import subprocess
import os
import re
import math
import tempfile
from bpy.props import (BoolProperty, IntProperty, FloatProperty, EnumProperty,
                       PointerProperty, StringProperty, CollectionProperty,
                       FloatVectorProperty)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Vector


def ensure_pillow_installed():
    try:
        import PIL  # noqa: F401
    except ImportError:
        python_exe = sys.executable
        try:
            subprocess.check_call([python_exe, "-m", "ensurepip"])
            subprocess.check_call([python_exe, "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([python_exe, "-m", "pip", "install", "pillow"])
        except Exception:
            pass


ensure_pillow_installed()
from PIL import Image, ImageFilter, ImageChops  # noqa: E402

bl_info = {
    "name": "Cortex SpriteForge",
    "author": "Cortex",
    "version": (2, 1),
    "blender": (5, 1, 1),
    "location": "View3D > Sidebar > SpriteForge",
    "description": "Export sprites isométriques – personnages et assets",
    "category": "Render",
}

# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ══════════════════════════════════════════════════════════════════════════════

CAM_RIG_NAME  = "CortexCamRig"
SPOT_TOP_NAME = "SpriteForge_SpotTop"
ASSET_EXTENSIONS = {'.fbx', '.obj', '.glb', '.gltf', '.dae'}

# Noms des collections SpriteForge
COL_ROOT       = "SpriteForge"
COL_CHARACTERS = "SpriteForge Characters"
COL_ASSETS     = "SpriteForge Assets"
COL_CAMERA     = "SpriteForge Camera"


def _ensure_spriteforge_collections():
    """Crée la hiérarchie de collections SpriteForge si elle n'existe pas."""
    scene = bpy.context.scene
    root = bpy.data.collections.get(COL_ROOT)
    if not root:
        root = bpy.data.collections.new(COL_ROOT)
        scene.collection.children.link(root)
    for name in (COL_CHARACTERS, COL_ASSETS, COL_CAMERA):
        sub = bpy.data.collections.get(name)
        if not sub:
            sub = bpy.data.collections.new(name)
            root.children.link(sub)
    return root


def _add_to_collection(obj_names, col_name):
    """Déplace les objets indiqués vers la collection donnée."""
    _ensure_spriteforge_collections()
    col = bpy.data.collections.get(col_name)
    if not col:
        return
    for name in obj_names:
        obj = bpy.data.objects.get(name)
        if not obj:
            continue
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        col.objects.link(obj)


def _collection_has_objects(col_name):
    col = bpy.data.collections.get(col_name)
    return bool(col and list(col.all_objects))

# Multiplicateur résolution interne (supersampling) : on rend à sprite_size × ce
# facteur, puis on recadre et on réduit en LANCZOS → sprites nets et anti-aliasés.
# Rendre à la taille finale (×1) puis agrandir la sous-région recadrée donnait du flou.
INTERNAL_RENDER_MULT = 4

# Marge de sécurité caméra au rendu : dézoom temporaire pour garantir que le
# personnage ne touche JAMAIS les bords du cadre, quelle que soit la direction ou
# la frame. L'espace vide ajouté est ensuite retiré par l'autocrop. 1.25 = +25 % de champ.
RENDER_SAFETY_ZOOM = 1.25

_CAM_SIZE_UPDATING = False

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS CAMÉRA
# ══════════════════════════════════════════════════════════════════════════════

def _get_cam_rig():
    return bpy.data.objects.get(CAM_RIG_NAME)

def _camera_rig_exists():
    rig = _get_cam_rig()
    return bool(rig and any(c.type == 'CAMERA' for c in rig.children))

def _apply_cam_width(settings, context):
    global _CAM_SIZE_UPDATING
    if _CAM_SIZE_UPDATING:
        return
    cam = context.scene.camera
    if cam and cam.data and cam.data.type == 'ORTHO':
        cam.data.ortho_scale = settings.cam_view_width

def _apply_cam_height(settings, context):
    global _CAM_SIZE_UPDATING
    if _CAM_SIZE_UPDATING:
        return
    cam = context.scene.camera
    if not (cam and cam.data and cam.data.type == 'ORTHO'):
        return
    # Rendu carré → aspect = 1
    if settings.cam_view_height > 0:
        cam.data.ortho_scale = max(0.1, settings.cam_view_height)

def _sync_cam_size_props(context):
    global _CAM_SIZE_UPDATING
    cam = context.scene.camera
    if not (cam and cam.data and cam.data.type == 'ORTHO'):
        return
    _CAM_SIZE_UPDATING = True
    props = context.scene.omni_render_settings
    w = cam.data.ortho_scale
    props.cam_view_width  = w
    props.cam_view_height = w  # rendu carré → même valeur
    _CAM_SIZE_UPDATING = False

def _apply_cam_offset(self, context):
    rig = _get_cam_rig()
    if rig:
        rig.location.x = self.cam_offset_x
        rig.location.y = self.cam_offset_y
        rig.location.z = self.cam_offset_z


def _on_mode_change(self, context):
    spot = bpy.data.objects.get(SPOT_TOP_NAME)
    if spot:
        hide = (self.export_mode != 'ASSETS')
        spot.hide_viewport = hide
        spot.hide_render = hide

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS QUALITÉ RENDU (matériaux & lumières)
# ══════════════════════════════════════════════════════════════════════════════

LIGHT_MAIN_NAME = "SpriteForge_Light"

# Entrées du Principled BSDF neutralisées en mode "couleurs mates" → (nom, valeur plate)
_FLAT_BSDF_INPUTS = (
    ("Metallic", 0.0),             # supprime l'aspect métallique (assets Meshy)
    ("Specular IOR Level", 0.0),   # supprime les reflets spéculaires brillants
)

def _set_flat_colors(enable):
    """Active/désactive le mode couleurs mates sur TOUS les matériaux à nœuds.
    Réversible et idempotent : la valeur d'origine est mémorisée sur le nœud
    (id-property) pour pouvoir être restaurée. Conserve la Base Color (texture)."""
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type != 'BSDF_PRINCIPLED':
                continue
            for name, flat_val in _FLAT_BSDF_INPUTS:
                inp = node.inputs.get(name)
                if inp is None:
                    continue
                key = "_sf_orig_" + name.replace(" ", "_")
                if enable:
                    if key not in node:
                        node[key] = inp.default_value
                    inp.default_value = flat_val
                else:
                    if key in node:
                        inp.default_value = node[key]
                        del node[key]

def _update_flat_colors(self, context):
    _set_flat_colors(self.flat_colors)

def _set_light_state(obj_name, enabled, energy):
    obj = bpy.data.objects.get(obj_name)
    if not obj:
        return
    obj.hide_render   = not enabled
    obj.hide_viewport = not enabled
    if obj.data and energy is not None:
        obj.data.energy = energy

def _update_main_light(self, context):
    _set_light_state(LIGHT_MAIN_NAME, self.main_light_on, self.main_light_energy)

def _update_spot_light(self, context):
    # Le spot ne doit rester visible qu'en mode ASSETS
    enabled = self.spot_light_on and (self.export_mode == 'ASSETS')
    _set_light_state(SPOT_TOP_NAME, enabled, self.spot_light_energy)

def _update_shadows(self, context):
    """Active/désactive globalement les ombres EEVEE."""
    try:
        context.scene.eevee.use_shadows = self.cast_shadows
    except Exception:
        pass

def _configure_top_spot(spot_data, spot_obj, energy):
    """Configure le spot supérieur pour un éclairage doux et flatteur des assets :
    placé en avant-haut (côté caméra), visant l'origine, cône large à bords dégradés.
    → éclaire la face visible + le dessus, sans point chaud ni bords durs."""
    spot_data.energy           = energy
    spot_data.spot_size        = math.radians(110)   # cône large
    spot_data.spot_blend       = 0.6                 # bords doux
    spot_data.shadow_soft_size = 3.0                 # ombres douces
    # Avant-haut, orienté vers l'origine (≈ 26.6° d'inclinaison)
    spot_obj.location      = (0.0, -6.0, 12.0)
    spot_obj.rotation_euler = (math.radians(26.57), 0.0, 0.0)

def _apply_quality_settings(scene):
    """Applique TOUS les réglages qualité (couleurs mates, ombres, lumières) depuis
    les propriétés. Indispensable au chargement du fichier : les callbacks `update`
    ne se déclenchent pas tout seuls → on force l'application ici."""
    props = getattr(scene, "omni_render_settings", None)
    if props is None:
        return
    _set_flat_colors(props.flat_colors)
    try:
        scene.eevee.use_shadows = props.cast_shadows
    except Exception:
        pass
    _set_light_state(LIGHT_MAIN_NAME, props.main_light_on, props.main_light_energy)
    spot_enabled = props.spot_light_on and (props.export_mode == 'ASSETS')
    _set_light_state(SPOT_TOP_NAME, spot_enabled, props.spot_light_energy)
    # Réapplique l'état pixel art (grille + réglages rendu nets) au chargement
    try:
        _apply_pixel_art_state(scene)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
#  PIXEL ART  (essentiel condensé depuis « Blender Pixel Kit » de SouthernShotty)
# ══════════════════════════════════════════════════════════════════════════════
#
#  On ne garde que le strict nécessaire pour un rendu pixel art, en RÉUTILISANT la
#  caméra et le pipeline d'export SpriteForge existants :
#    1. Anti-aliasing coupé  → render.filter_size = 0          → pixels nets.
#    2. Vue colorimétrique « Standard »                        → couleurs plates.
#    3. Réduction finale en NEAREST (au lieu de LANCZOS)       → bords francs.
#    4. Grille de prévisualisation projetée sur LA MÊME caméra → on visualise la
#       densité de pixels directement dans le viewport (background image FRONT).
#  Volontairement écartés : caméras BPK dédiées, compositor, outline, palettes.

PIXEL_GRID_PREFIX   = "SF Pixel Grid"
PIXEL_GRID_MAX_SIZE = 1024

# Config pixel art active pendant un export, ou None hors pixel art. Renseignée en
# tête de render_spin / render_asset par _set_pixel_cfg(), lue par _apply_bbox_and_size().
_PIXEL_CFG = None


def _set_pixel_cfg(settings):
    """Prépare (ou efface) la config pixel art utilisée par le post-traitement PIL."""
    global _PIXEL_CFG
    if settings and settings.pixel_art_enabled:
        col = settings.pixel_outline_color
        _PIXEL_CFG = {
            "resolution":        max(2, int(settings.pixel_resolution)),
            "antialias":         settings.pixel_antialias,
            "outline":           settings.pixel_outline,
            "outline_thickness": int(settings.pixel_outline_thickness),
            "outline_color":     (int(col[0] * 255), int(col[1] * 255), int(col[2] * 255)),
            "dither":            settings.pixel_dither,
            "dither_intensity":  settings.pixel_dither_intensity,
        }
    else:
        _PIXEL_CFG = None


def _apply_dither(img, intensity):
    """Réduction de palette avec tramage Floyd–Steinberg (look rétro). L'intensité
    (0→1) diminue le nombre de couleurs : 64 (subtil) → 4 (fort). Alpha préservé."""
    img = img.convert("RGBA")
    alpha = img.split()[3]
    colors = max(2, int(round(64 - intensity * 60)))
    rgb = img.convert("RGB")
    dithered = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT,
                            dither=Image.Dither.FLOYDSTEINBERG).convert("RGBA")
    dithered.putalpha(alpha)
    return dithered


def _apply_outline(img, thickness, color):
    """Ajoute un contour de `thickness` px (couleur RGB 0-255) autour des pixels opaques.
    Dilate le masque alpha puis compose le sprite par-dessus. Agrandit le canvas de
    `thickness` de chaque côté pour ne pas rogner le contour."""
    if thickness <= 0:
        return img
    img = img.convert("RGBA")
    pad = thickness
    w, h = img.size
    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    canvas.paste(img, (pad, pad))
    img = canvas
    mask = img.split()[3].point(lambda a: 255 if a > 16 else 0)
    dil = mask
    for _ in range(thickness):
        dil = dil.filter(ImageFilter.MaxFilter(3))
    outline_mask = ImageChops.subtract(dil, mask)
    layer = Image.new("RGBA", img.size, color + (0,))
    layer.putalpha(outline_mask)
    return Image.alpha_composite(layer, img)


def _is_pixel_grid(image):
    return bool(image) and (image.name.startswith(PIXEL_GRID_PREFIX)
                            or image.name.startswith("BPK Pixel Grid"))


def _pixel_grid_image(resolution):
    """Crée/récupère une texture de grille carrée resolution×resolution : lignes fines
    sur fond transparent, agrandie à ≈1024 px pour rester nette dans le viewport.
    Condensé de blender_pixel_kit/utils/grid_images.py."""
    resolution = max(1, int(resolution))
    cell = max(1, PIXEL_GRID_MAX_SIZE // resolution)
    size = resolution * cell
    name = f"{PIXEL_GRID_PREFIX} {resolution}"
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) == (size, size):
        return img
    if img is None:
        img = bpy.data.images.new(name=name, width=size, height=size,
                                  alpha=True, float_buffer=False)
    else:
        img.scale(size, size)
    pixels = [0.0] * (size * size * 4)
    for y in range(size):
        for x in range(size):
            is_line   = (x % cell == 0) or (y % cell == 0)
            is_border = (x == size - 1) or (y == size - 1)
            if not is_line and not is_border:
                continue
            i = (y * size + x) * 4
            pixels[i] = pixels[i + 1] = pixels[i + 2] = 1.0
            pixels[i + 3] = 0.7 if is_border else 0.4
    img.pixels.foreach_set(pixels)
    img.pack()
    return img


def _attach_pixel_grid(cam_data, resolution, opacity):
    """Projette la grille pixel sur la caméra (background image en avant-plan).
    Retire d'abord toute grille existante pour ne pas les empiler."""
    if cam_data is None:
        return
    img = _pixel_grid_image(resolution)
    for bg in list(cam_data.background_images):
        if bg.image is None or _is_pixel_grid(bg.image):
            cam_data.background_images.remove(bg)
    cam_data.show_background_images = True
    bg = cam_data.background_images.new()
    bg.image        = img
    bg.alpha        = opacity
    bg.frame_method = 'FIT'
    if hasattr(bg, "display_depth"):
        bg.display_depth = 'FRONT'


def _clear_pixel_grids():
    """Retire toutes les grilles pixel de toutes les caméras."""
    for cam in bpy.data.cameras:
        removed = False
        for bg in list(cam.background_images):
            if bg.image is None or _is_pixel_grid(bg.image):
                cam.background_images.remove(bg)
                removed = True
        if removed and not cam.background_images:
            cam.show_background_images = False
        cam.update_tag()


def _set_pixel_grid_opacity(opacity):
    for cam in bpy.data.cameras:
        for bg in cam.background_images:
            if _is_pixel_grid(bg.image):
                bg.alpha = opacity


def _apply_pixel_render_settings(scene, enable):
    """Active/désactive les réglages de rendu nets. Sauvegarde puis restaure les
    valeurs d'origine (filter_size, view_transform) via des id-properties de scène."""
    render = scene.render
    props = getattr(scene, "omni_render_settings", None)
    if enable:
        if "_sf_orig_filter_size" not in scene:
            scene["_sf_orig_filter_size"] = render.filter_size
        if "_sf_orig_view_transform" not in scene:
            scene["_sf_orig_view_transform"] = scene.view_settings.view_transform
        # Anti-aliasing du rendu : 0 = pixels nets, 1.5 (défaut Blender) = bords lissés.
        render.filter_size           = 1.5 if (props and props.pixel_antialias) else 0.0
        render.resolution_percentage = 100
        try:
            scene.view_settings.view_transform = 'Standard'
        except Exception:
            pass
    else:
        if "_sf_orig_filter_size" in scene:
            render.filter_size = scene["_sf_orig_filter_size"]
            del scene["_sf_orig_filter_size"]
        if "_sf_orig_view_transform" in scene:
            try:
                scene.view_settings.view_transform = scene["_sf_orig_view_transform"]
            except Exception:
                pass
            del scene["_sf_orig_view_transform"]


def _redraw_view3d(context):
    if context and getattr(context, "screen", None):
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _set_viewport_camera_view(context):
    """Bascule les viewports 3D en vue caméra (pour voir la grille sur le cadre)."""
    screen = getattr(context, "screen", None)
    if not screen:
        return
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.region_3d.view_perspective = 'CAMERA'
        area.tag_redraw()


def _apply_pixel_art_state(scene, context=None):
    """Applique tout l'état pixel art depuis les props : réglages rendu nets + grille
    sur LA MÊME caméra (scene.camera). Bascule en vue caméra si un contexte est fourni."""
    props = getattr(scene, "omni_render_settings", None)
    if props is None:
        return
    _apply_pixel_render_settings(scene, props.pixel_art_enabled)
    cam = scene.camera
    if props.pixel_art_enabled and props.pixel_show_grid and cam and cam.data:
        _attach_pixel_grid(cam.data, int(props.pixel_resolution), props.pixel_grid_opacity)
    else:
        _clear_pixel_grids()
    if context is not None:
        if props.pixel_art_enabled:
            _set_viewport_camera_view(context)
        _redraw_view3d(context)


def _update_pixel_art(self, context):
    if context is not None:
        _apply_pixel_art_state(context.scene, context)


def _update_pixel_grid_opacity(self, context):
    _set_pixel_grid_opacity(self.pixel_grid_opacity)
    _redraw_view3d(context)

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _short_action_name(action_name):
    parts = action_name.split('|')
    IGNORE = {'baselayer', 'layer0', 'take 001', 'take001'}
    ARMATURE_PREFIX = ('armature', 'rig', 'metarig')
    meaningful = [
        p for p in parts
        if p and p.lower() not in IGNORE
        and not any(p.lower().startswith(a) for a in ARMATURE_PREFIX)
    ]
    if meaningful:
        return meaningful[-1]
    for p in reversed(parts):
        if p and p.lower() not in IGNORE:
            return p
    return action_name

# Cache des items d'enum d'actions : Blender NE garde PAS de référence aux chaînes
# retournées par un callback d'EnumProperty → si elles sont collectées par le GC,
# l'UI affiche des libellés corrompus ou plante. On conserve la dernière liste ici.
_ACTION_ENUM_CACHE = [("NONE", "Aucune action", "")]


def _character_armatures():
    """Armatures réellement importées = celles de la collection SpriteForge Characters
    (source de vérité). Repli sur toutes les armatures si la collection est absente.
    Exclut ainsi les actions orphelines accumulées dans le .blend (anciens imports)."""
    col = bpy.data.collections.get(COL_CHARACTERS)
    if col:
        arms = [o for o in col.all_objects if o.type == 'ARMATURE']
        if arms:
            return arms
    return _all_armatures()


def _action_items_callback(self, context):
    """Items du menu « Action à exporter ». UNE entrée par personnage importé (armature
    de la collection), libellé = « sous-dossier / nom du FBX ». L'identifiant reste
    action.name (utilisé tel quel par l'export spécifique). Les actions orphelines du
    .blend (non rattachées à un personnage importé) sont volontairement exclues."""
    global _ACTION_ENUM_CACHE
    raw  = []   # (clé_tri, identifiant, libellé, description)
    seen = set()
    for arm in _character_armatures():
        action = arm.animation_data.action if arm.animation_data else None
        if action is None or action.name in seen:
            continue
        seen.add(action.name)
        disp = (arm.get("sf_display_name", "") or action.get("sf_display_name", "")
                or _short_action_name(action.name))
        sdir = arm.get("sf_source_dir", "") or action.get("sf_source_dir", "")
        label = f"{sdir} / {disp}" if sdir else disp
        raw.append(((sdir.lower(), disp.lower()), action.name, label, disp))

    raw.sort(key=lambda x: x[0])
    items = [(idn, lbl, desc) for _, idn, lbl, desc in raw]
    _ACTION_ENUM_CACHE = items or [("NONE", "Aucune action", "")]
    return _ACTION_ENUM_CACHE

def _sync_frames_from_action(settings, context):
    action = bpy.data.actions.get(settings.selected_action)
    if action:
        settings.frame_start = int(action.frame_range[0])
        settings.frame_end   = int(action.frame_range[1])

def _display_name_for_action(context, action_name):
    # Priorité au nom du FBX d'origine persisté sur l'action (= nom listé au scan).
    action = bpy.data.actions.get(action_name)
    if action is not None:
        disp = action.get("sf_display_name", "")
        if disp:
            return disp
    if context:
        for item in context.scene.omni_anim_items:
            if item.action_name == action_name:
                return item.display_name
    return _short_action_name(action_name)


def _source_dir_for_action(context, action, arm):
    """Sous-dossier de provenance d'une action (persisté sur l'action ou l'armature
    à l'import, avec repli sur la liste de scan tant qu'elle n'est pas vidée)."""
    if action is not None:
        sd = action.get("sf_source_dir", "")
        if sd:
            return sd
    if arm is not None:
        sd = arm.get("sf_source_dir", "")
        if sd:
            return sd
    if context and action is not None:
        for item in context.scene.omni_anim_items:
            if item.action_name == action.name and item.source_dir:
                return item.source_dir
    return ""


def _export_subdirs(context, is_chars):
    """Liste triée des sous-dossiers de provenance distincts (non vides) des éléments
    importés — repli sur la liste de scan tant qu'elle n'est pas vidée. Sert à grouper
    l'export sous Render/<sous-dossier>/ et à décider l'affichage de la coche."""
    dirs = set()
    if is_chars:
        for arm in _character_armatures():
            sd = arm.get("sf_source_dir", "")
            if not sd:
                act = arm.animation_data.action if arm.animation_data else None
                sd = act.get("sf_source_dir", "") if act else ""
            if sd:
                dirs.add(sd)
        if not dirs and context:
            for it in context.scene.omni_anim_items:
                if it.source_dir:
                    dirs.add(it.source_dir)
    else:
        col = bpy.data.collections.get(COL_ASSETS)
        if col:
            for obj in col.all_objects:
                sd = obj.get("sf_source_dir", "")
                if sd:
                    dirs.add(sd)
        if not dirs and context:
            for it in context.scene.omni_asset_items:
                if it.source_dir:
                    dirs.add(it.source_dir)
    return sorted(dirs, key=str.lower)

def _get_base_path(settings):
    return bpy.path.abspath(settings.output_path) if settings.output_path else os.path.dirname(bpy.data.filepath)

def _import_base_dir(settings):
    """Dossier d'import racine du mode courant (les sous-dossiers en sont relatifs)."""
    raw = settings.assets_dir if settings.export_mode == 'ASSETS' else settings.input_dir
    return bpy.path.abspath(raw).rstrip("\\/") if raw else ""

def _element_render_dir(settings, source_dir, fallback_base):
    """Dossier 'Render' cible d'un élément.
    - Mode sous-dossiers actif + provenance connue : on entre dans le sous-dossier
      d'origine (sous le dossier d'import) et on y crée Render → <import>/<src>/Render/.
    - Sinon : <fallback_base>/Render/ (comportement classique, dossier unique)."""
    if settings.export_to_subdirs and source_dir:
        base = _import_base_dir(settings) or fallback_base
        return os.path.join(base, source_dir, "Render")
    return os.path.join(fallback_base, "Render")

def _dir_label(i, num_dirs):
    return f"{round(360 / num_dirs * i)}°"

def _selected_dir_indices(mask, num_dirs):
    return [i for i in range(num_dirs) if mask & (1 << i)]

def _asset_enum_callback(self, context):
    items = []
    col = bpy.data.collections.get(COL_ASSETS)
    if col:
        seen = set()
        for obj in col.all_objects:
            name = obj.get("sf_asset_name", "")
            if name and name not in seen:
                seen.add(name)
                items.append((obj.name, name, ""))
    if not items and context:
        for i, item in enumerate(context.scene.omni_asset_items):
            if item.display_name:
                items.append((str(i), item.display_name, ""))
    return items or [("-1", "Aucun asset", "")]


# ── Scan + auto-remplissage des listes selon le dossier ───────────────────────

def _populate_anim_items(context):
    """(Re)scanne le dossier d'import perso et remplit omni_anim_items.
    Vide la liste si le dossier est absent/invalide. Renseigne l'adresse d'export
    par défaut (= dossier d'import) si elle n'a pas encore été personnalisée."""
    props = context.scene.omni_render_settings
    items = context.scene.omni_anim_items
    items.clear()
    directory = bpy.path.abspath(props.input_dir).rstrip("\\/") if props.input_dir else ""
    if not directory or not os.path.isdir(directory):
        props.import_status = ""
        return False
    mode, merged, anims, textures = _scan_fbx_dir(directory)
    if mode == "merged":
        item = items.add()
        item.display_name = os.path.splitext(os.path.basename(merged))[0]
        item.fbx_path     = merged
        item.source_dir   = ""
        item.is_merged    = True
        props.import_status = f"Fichier fusionné · {len(textures)} textures"
    else:
        for fname, fpath in anims:
            rel = os.path.relpath(os.path.dirname(fpath), directory)
            item = items.add()
            item.display_name = os.path.splitext(fname)[0]
            item.fbx_path     = fpath
            item.source_dir   = "" if rel in (".", "") else rel
            item.is_merged    = False
        props.import_status = f"{len(anims)} animation(s) · {len(textures)} textures"
    # Adresse d'export par défaut = dossier d'import (tant que non personnalisée)
    if not props.output_path:
        props.output_path = props.input_dir
    return True

def _populate_asset_items(context):
    """(Re)scanne le dossier d'assets et remplit omni_asset_items.
    Vide la liste si le dossier est absent/invalide. Renseigne l'adresse d'export
    par défaut (= dossier d'assets) si elle n'a pas encore été personnalisée."""
    props = context.scene.omni_render_settings
    items = context.scene.omni_asset_items
    items.clear()
    directory = bpy.path.abspath(props.assets_dir).rstrip("\\/") if props.assets_dir else ""
    if not directory or not os.path.isdir(directory):
        props.assets_status = ""
        return False
    found = []
    for root, _, files in os.walk(directory):
        for f in files:
            if os.path.splitext(f)[1].lower() in ASSET_EXTENSIONS:
                full = os.path.join(root, f)
                rel  = os.path.relpath(root, directory)
                src  = "" if rel in (".", "") else rel
                found.append((src, f, full))
    found.sort(key=lambda x: (x[0].lower(), x[1].lower()))
    for src, f, full in found:
        item = items.add()
        item.display_name = os.path.splitext(f)[0]
        item.file_path    = full
        item.source_dir   = src
    props.assets_status = f"{len(found)} asset(s) trouvé(s)"
    # Assets : l'adresse d'export suit toujours l'adresse d'import dès sa mise à jour
    props.output_path = props.assets_dir
    return True

def _on_input_dir_update(self, context):
    _populate_anim_items(context)

def _on_assets_dir_update(self, context):
    _populate_asset_items(context)

# ══════════════════════════════════════════════════════════════════════════════
#  PROPERTY GROUPS
# ══════════════════════════════════════════════════════════════════════════════

class OmniAnimItem(PropertyGroup):
    display_name: StringProperty(name="Name")
    fbx_path:     StringProperty(name="FBX Path")
    action_name:  StringProperty(name="Action")
    arm_name:     StringProperty(name="Armature")
    source_dir:   StringProperty(name="Source Dir", default="")
    is_merged:    BoolProperty(default=False)
    imported:     BoolProperty(default=False)


class OmniAssetItem(PropertyGroup):
    display_name: StringProperty(name="Name")
    file_path:    StringProperty(name="File Path")
    source_dir:   StringProperty(name="Source Dir", default="")
    imported:     BoolProperty(default=False)


class OmniRenderSettings(PropertyGroup):
    export_mode: EnumProperty(
        name="Mode",
        items=[
            ("CHARACTERS", "Personnages", "Export d'animations de personnages"),
            ("ASSETS",     "Assets",      "Export d'assets statiques"),
        ],
        default="CHARACTERS",
        update=_on_mode_change,
    )

    # ── Import personnages ─────────────────────────────────────
    input_dir:     StringProperty(name="Dossier", default="", subtype='DIR_PATH',
                                  update=_on_input_dir_update)
    import_status: StringProperty(name="", default="")

    # ── Assets ────────────────────────────────────────────────
    assets_dir:    StringProperty(name="Dossier", default="", subtype='DIR_PATH',
                                  update=_on_assets_dir_update)
    assets_status: StringProperty(name="", default="")
    selected_dirs_mask: IntProperty(name="Directions sélectionnées", default=0xFF,
                                    description="Masque des directions à exporter (bit i = direction i)")
    selected_asset_idx: EnumProperty(name="Asset", items=_asset_enum_callback)

    # ── Taille sprite (unique, dimension max) ─────────────────
    sprite_size: EnumProperty(
        name="Taille sprite",
        items=[
            ("32",   "32 px",   ""),
            ("48",   "48 px",   ""),
            ("64",   "64 px",   ""),
            ("96",   "96 px",   ""),
            ("128",  "128 px",  ""),
            ("192",  "192 px",  ""),
            ("256",  "256 px",  ""),
            ("384",  "384 px",  ""),
            ("512",  "512 px",  ""),
            ("1024", "1024 px", ""),
        ],
        default="256",
        description="Dimension maximale du sprite final. Le rendu interne est 4× plus grand, puis crop auto + redimensionnement."
    )

    # ── Frames ────────────────────────────────────────────────
    frame_start:       IntProperty(name="Début", default=1,  min=0)
    frame_end:         IntProperty(name="Fin",   default=16, min=0)
    frame_step:        IntProperty(name="Step",  default=1,  min=1)
    force_frame_count: BoolProperty(name="Limiter nb frames", default=True)
    forced_frames:     IntProperty(name="Frames cibles", default=30, min=1)
    gif_duration:      IntProperty(name="ms/frame", default=100, min=1)

    # ── Export ────────────────────────────────────────────────
    num_directions: IntProperty(name="Directions", default=8, min=1)
    output_path:    StringProperty(name="Sortie", default="", subtype='DIR_PATH')
    export_to_subdirs: BoolProperty(
        name="Exporter dans les sous-dossiers d'origine", default=True,
        description="Si les éléments ont été importés depuis des sous-dossiers, exporte "
                    "chaque rendu dans Render/<sous-dossier d'origine>/ (un dossier par "
                    "provenance) au lieu d'un unique dossier Render.",
    )
    selected_action: EnumProperty(
        name="Action",
        items=_action_items_callback,
        update=lambda self, ctx: _sync_frames_from_action(self, ctx)
    )
    export_format: EnumProperty(
        name="Format",
        items=[
            ("Single",      "Images individuelles", "Un PNG par frame et par direction"),
            ("Spritesheet", "Spritesheet",           "Toutes les directions en une image"),
            ("GIF_DIR",     "GIF par direction",     "Un GIF par direction"),
            ("GIF_ONE",     "GIF unique",             "Un GIF toutes directions"),
        ],
        default="GIF_DIR"
    )
    transparent_bg: BoolProperty(name="Fond transparent", default=True)
    export_normals: BoolProperty(name="Normal maps",      default=False)

    # ── Qualité du rendu (matériaux & lumières) ───────────────
    flat_colors: BoolProperty(
        name="Couleurs mates (anti-métallique)", default=True,
        description="Neutralise le métallique et les reflets spéculaires des matériaux "
                    "(idéal pour les assets Meshy AI). Conserve les couleurs/textures de base.",
        update=_update_flat_colors,
    )
    cast_shadows: BoolProperty(
        name="Ombres", default=False,
        description="Active ou désactive les ombres portées (EEVEE) au rendu",
        update=_update_shadows,
    )
    main_light_on: BoolProperty(
        name="Lumière principale", default=True,
        description="Active ou masque la lumière principale",
        update=_update_main_light,
    )
    main_light_energy: FloatProperty(
        name="Intensité", default=1000.0, min=0.0, max=50000.0,
        description="Puissance de la lumière principale (W)",
        update=_update_main_light,
    )
    spot_light_on: BoolProperty(
        name="Spot supérieur", default=True,
        description="Active ou masque le spot supérieur (assets)",
        update=_update_spot_light,
    )
    spot_light_energy: FloatProperty(
        name="Intensité spot", default=1500.0, min=0.0, max=50000.0,
        description="Puissance du spot supérieur (W)",
        update=_update_spot_light,
    )

    # ── Caméra ────────────────────────────────────────────────
    cam_view_width: FloatProperty(
        name="Largeur", default=7.258, min=0.1, max=200.0,
        description="Largeur du champ de vision en unités Blender. Modifie l'ortho_scale.",
        update=lambda self, ctx: _apply_cam_width(self, ctx)
    )
    cam_view_height: FloatProperty(
        name="Hauteur", default=7.258, min=0.1, max=200.0,
        description="Hauteur du champ de vision en unités Blender.",
        update=lambda self, ctx: _apply_cam_height(self, ctx)
    )
    cam_pan_step: FloatProperty(name="Pas", default=0.5, min=0.01)

    # ── Position pivot caméra (offsets X/Y/Z) ────────────────
    cam_offset_x: FloatProperty(
        name="X", default=0.0, min=-100.0, max=100.0, step=10,
        description="Décalage horizontal du pivot caméra (axe X)",
        update=_apply_cam_offset,
    )
    cam_offset_y: FloatProperty(
        name="Y", default=0.0, min=-100.0, max=100.0, step=10,
        description="Décalage avant/arrière du pivot caméra (axe Y)",
        update=_apply_cam_offset,
    )
    cam_offset_z: FloatProperty(
        name="Z", default=0.0, min=-100.0, max=100.0, step=10,
        description="Décalage vertical du pivot caméra (axe Z)",
        update=_apply_cam_offset,
    )

    # ── Pixel Art (inspiré de Blender Pixel Kit) ──────────────
    pixel_art_enabled: BoolProperty(
        name="Rendu pixel art", default=False,
        description="Active le rendu pixel art : pixels nets (anti-aliasing coupé), "
                    "couleurs plates (vue Standard), réduction NEAREST et grille de "
                    "prévisualisation sur la caméra. Réutilise la caméra SpriteForge.",
        update=_update_pixel_art,
    )
    pixel_resolution: EnumProperty(
        name="Résolution pixel",
        items=[
            ("16",  "16 px",  "Grille 16 × 16"),
            ("32",  "32 px",  "Grille 32 × 32"),
            ("48",  "48 px",  "Grille 48 × 48"),
            ("64",  "64 px",  "Grille 64 × 64"),
            ("96",  "96 px",  "Grille 96 × 96"),
            ("128", "128 px", "Grille 128 × 128"),
            ("256", "256 px", "Grille 256 × 256"),
        ],
        default="256",
        description="Densité de la grille pixel art affichée sur la caméra "
                    "(nombre de cellules sur la dimension du cadre).",
        update=_update_pixel_art,
    )
    pixel_show_grid: BoolProperty(
        name="Afficher la grille", default=True,
        description="Affiche la grille pixel sur la caméra dans le viewport.",
        update=_update_pixel_art,
    )
    pixel_grid_opacity: FloatProperty(
        name="Opacité grille", default=0.35, min=0.0, max=1.0, subtype="FACTOR",
        description="Opacité de la grille pixel affichée sur la caméra.",
        update=_update_pixel_grid_opacity,
    )
    pixel_antialias: BoolProperty(
        name="Anti-aliasing", default=False,
        description="Lisse les bords (rendu filter_size 1.5 + réduction LANCZOS). "
                    "Décoché = pixels durs et nets, look pixel art classique.",
        update=_update_pixel_art,
    )
    pixel_outline: BoolProperty(
        name="Contour", default=False,
        description="Ajoute un contour autour du sprite (appliqué à l'export et l'aperçu).",
    )
    pixel_outline_thickness: IntProperty(
        name="Épaisseur", default=1, min=1, max=8,
        description="Épaisseur du contour, en pixels de la grille pixel art.",
    )
    pixel_outline_color: FloatVectorProperty(
        name="Couleur", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0),
        description="Couleur du contour (noir par défaut).",
    )
    pixel_dither: BoolProperty(
        name="Dithering", default=False,
        description="Tramage Floyd–Steinberg + réduction de palette (look rétro). "
                    "Appliqué à l'export et l'aperçu.",
    )
    pixel_dither_intensity: FloatProperty(
        name="Intensité", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
        description="Force du dithering : faible = palette riche (subtil), "
                    "forte = peu de couleurs (tramage marqué).",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  IMPORT UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

_ANIM_NAME_OVERRIDES = {
    "Walking":                "Walk",
    "Running":                "Run",
    "Male_Bend_Over_Pick_Up": "Pickup",
}

def _extract_anim_name(filename):
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r'[_\-]?[Ww]ith[Ss]kin$', '', stem)
    m = re.search(r'_Animation_(.+)$', stem)
    if m:
        part = re.sub(r'_\d+$', '', m.group(1))
    else:
        parts = stem.split('_')
        part = parts[-1] if parts else stem
    return _ANIM_NAME_OVERRIDES.get(part, part)

def _scan_fbx_dir(directory):
    all_fbx, textures = [], []
    for root, _, files in os.walk(directory):
        for f in sorted(files):
            p = os.path.join(root, f)
            if f.lower().endswith('.fbx'):
                all_fbx.append((f, p))
            elif f.lower().endswith('.png'):
                textures.append(p)
    all_fbx.sort(key=lambda x: (
        os.path.relpath(os.path.dirname(x[1]), directory).lower(),
        x[0].lower()
    ))
    merged = [(n, p) for n, p in all_fbx if re.search(r'merged|mergedanim|all.?anim', n, re.I)]
    if merged:
        return "merged", merged[0][1], [], textures
    anims = [(f, p) for f, p in all_fbx if not re.search(r'character.?output', f, re.I)]
    if not anims:
        anims = list(all_fbx)
    return "separate", None, anims, textures

def _do_import_fbx(path):
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.fbx(filepath=path)
    return list(set(bpy.data.objects.keys()) - before)

def _find_armature_in(names):
    for n in names:
        o = bpy.data.objects.get(n)
        if o and o.type == 'ARMATURE':
            return o
    return None

def _find_armature_for_action(action):
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.animation_data and obj.animation_data.action == action:
            return obj
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE':
            return obj
    return None

def _get_mesh_children(armature):
    return [o for o in bpy.data.objects if o.type == 'MESH' and o.parent == armature]

def _all_armatures():
    return [o for o in bpy.data.objects if o.type == 'ARMATURE']

def _set_render_visibility(show_arm, all_armatures):
    restore = {}
    show_meshes = set(_get_mesh_children(show_arm)) | {show_arm}
    for arm in all_armatures:
        for obj in list(_get_mesh_children(arm)) + [arm]:
            restore[obj.name] = obj.hide_render
            obj.hide_render = (obj not in show_meshes)
    return restore

def _restore_render_visibility(restore):
    for name, val in restore.items():
        obj = bpy.data.objects.get(name)
        if obj:
            obj.hide_render = val


# ══════════════════════════════════════════════════════════════════════════════
#  RENDER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_name(name):
    return "".join(c if c not in r'\/:*?"<>|' else "_" for c in name)

def get_effective_step(settings):
    if settings.force_frame_count:
        total = max(1, settings.frame_end - settings.frame_start)
        return max(1, math.ceil(total / settings.forced_frames))
    return max(1, settings.frame_step)

# Marge de sécurité en pixels autour du contenu pour éviter que le crop effleure les bords
_BBOX_MARGIN = 6

def _get_global_bbox(frames_per_dir):
    """Bbox union sur TOUTES les directions et TOUTES les frames (pixels non-transparents).
    Chaque frame contribue sa propre bbox ; on prend le min des bords gauche/haut
    et le max des bords droite/bas → rectangle englobant le contenu le plus grand.
    Une marge de sécurité est ajoutée pour éviter les effets de bord."""
    all_bboxes = []
    ref_w = ref_h = 0
    for frames in frames_per_dir.values():
        for img in frames:
            img.load()  # force lecture pixel (PIL est lazy)
            rgba = img if img.mode == 'RGBA' else img.convert('RGBA')
            bbox = rgba.split()[3].getbbox()
            if bbox:
                all_bboxes.append(bbox)
            if ref_w == 0:
                ref_w, ref_h = img.size
    if not all_bboxes:
        return None
    left   = max(0,     min(b[0] for b in all_bboxes) - _BBOX_MARGIN)
    upper  = max(0,     min(b[1] for b in all_bboxes) - _BBOX_MARGIN)
    right  = min(ref_w, max(b[2] for b in all_bboxes) + _BBOX_MARGIN)
    lower  = min(ref_h, max(b[3] for b in all_bboxes) + _BBOX_MARGIN)
    return (left, upper, right, lower)

def _bbox_to_output_size(bbox, max_size):
    """Calcule (w, h) de sortie depuis une bbox, dimension max = max_size, ratio préservé.
    Appelé UNE seule fois par action → toutes les frames et directions partagent
    exactement la même taille canvas (montée ET descente vers max_size)."""
    w = max(1, bbox[2] - bbox[0])
    h = max(1, bbox[3] - bbox[1])
    if max_size <= 0:
        return (w, h)
    scale = max_size / max(w, h)
    return (max(1, round(w * scale)), max(1, round(h * scale)))

def _apply_bbox_and_size(img, bbox, out_w, out_h):
    """Crop vers bbox puis resize EXACTEMENT à (out_w, out_h).
    Toutes les frames reçoivent les mêmes arguments → taille canvas identique garantie.

    En mode pixel art (_PIXEL_CFG non None) : réduit d'abord le contenu à la grille
    `resolution` (vrais gros pixels), applique dither + contour à cette échelle, puis
    remonte à (out_w, out_h) en NEAREST → blocs nets. Sinon : simple resize LANCZOS."""
    if bbox:
        img = img.crop(bbox)

    cfg = _PIXEL_CFG
    if cfg is None:
        if img.size != (out_w, out_h):
            img = img.resize((out_w, out_h), Image.LANCZOS)
        return img

    # Taille de la grille pixel, ratio de sortie préservé.
    px = cfg["resolution"]
    if out_w >= out_h:
        gw = px
        gh = max(1, round(px * out_h / out_w))
    else:
        gh = px
        gw = max(1, round(px * out_w / out_h))

    # Réduction à la grille : LANCZOS (lissé) si anti-aliasing, NEAREST (dur) sinon.
    down = Image.LANCZOS if cfg["antialias"] else Image.NEAREST
    img = img.resize((gw, gh), down)

    if cfg["dither"]:
        img = _apply_dither(img, cfg["dither_intensity"])
    if cfg["outline"]:
        img = _apply_outline(img, cfg["outline_thickness"], cfg["outline_color"])

    # Remontée à la taille finale en NEAREST → gros pixels nets.
    if img.size != (out_w, out_h):
        img = img.resize((out_w, out_h), Image.NEAREST)
    return img

def rgba_to_gif_frame(img):
    rgba = img.convert("RGBA")
    rgb  = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba, mask=rgba.split()[3])
    quantized = rgb.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    old_palette = quantized.getpalette()
    new_palette = [0, 0, 0] + old_palette[:255 * 3]
    alpha      = rgba.split()[3]
    src_data   = list(quantized.tobytes())
    alpha_data = list(alpha.tobytes())
    new_data   = bytes([0 if alpha_data[i] < 128 else src_data[i] + 1 for i in range(len(src_data))])
    result     = Image.frombytes("P", rgba.size, new_data)
    result.putpalette(new_palette)
    return result

def save_gif(frames, path, transparent=True, duration=100):
    if not frames:
        return
    if transparent:
        converted = [rgba_to_gif_frame(f) for f in frames]
        converted[0].save(path, save_all=True, append_images=converted[1:],
                          optimize=False, duration=duration, loop=0, transparency=0, disposal=2)
    else:
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       optimize=False, duration=duration, loop=0)

def create_normal_map_material():
    mat = bpy.data.materials.get("Normal_Map_Material")
    if mat:
        return mat
    mat = bpy.data.materials.new(name="Normal_Map_Material")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    output   = nodes.new(type='ShaderNodeOutputMaterial')
    emission = nodes.new(type='ShaderNodeEmission')
    geometry = nodes.new(type='ShaderNodeNewGeometry')
    vt = nodes.new(type='ShaderNodeVectorTransform')
    vt.vector_type  = 'NORMAL'
    vt.convert_from = 'WORLD'
    vt.convert_to   = 'CAMERA'
    links.new(geometry.outputs['Normal'], vt.inputs['Vector'])
    links.new(vt.outputs['Vector'], emission.inputs['Color'])
    links.new(emission.outputs['Emission'], output.inputs['Surface'])
    mat.use_backface_culling = True
    mat.blend_method = 'OPAQUE'
    return mat

def assign_material_to_all_meshes(mat):
    original_mats = {}
    for obj in bpy.context.scene.objects:
        if obj.type == 'MESH':
            original_mats[obj.name] = [slot.material for slot in obj.material_slots]
            obj.data.materials.clear()
            obj.data.materials.append(mat)
    return original_mats

def restore_materials(original_mats):
    for obj_name, mats in original_mats.items():
        obj = bpy.data.objects.get(obj_name)
        if obj:
            obj.data.materials.clear()
            for mat in mats:
                obj.data.materials.append(mat)

def _apply_render_settings(settings, context):
    scene = context.scene
    # Rendu interne haute résolution : sprite_size × INTERNAL_RENDER_MULT (carré)
    internal = int(settings.sprite_size) * INTERNAL_RENDER_MULT
    scene.render.resolution_x = internal
    scene.render.resolution_y = internal
    scene.render.film_transparent = settings.transparent_bg
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode  = 'RGBA' if settings.transparent_bg else 'RGB'
    if settings.pixel_art_enabled:
        # filter_size : 0 = pixels nets, 1.5 = bords lissés (selon l'anti-aliasing).
        scene.render.filter_size = 1.5 if settings.pixel_antialias else 0.0
        try:
            scene.view_settings.view_transform = 'Standard'
        except Exception:
            pass
    try:
        scene.eevee.use_shadows = settings.cast_shadows
    except Exception:
        pass


def _get_action_fcurves(action):
    if hasattr(action, 'fcurves'):
        try:
            return list(action.fcurves)
        except Exception:
            pass
    fcurves = []
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    try:
                        cb = strip.channelbag(slot)
                        if cb:
                            fcurves.extend(cb.fcurves)
                    except Exception:
                        pass
    except Exception:
        pass
    return fcurves


def _action_has_root_motion(arm):
    if not arm or not arm.animation_data or not arm.animation_data.action:
        return False
    for fc in _get_action_fcurves(arm.animation_data.action):
        if fc.data_path == 'location':
            vals = [abs(kp.co[1]) for kp in fc.keyframe_points]
            if vals and max(vals) > 0.001:
                return True
    return False


def _make_animation_inplace(arm):
    if not arm.animation_data or not arm.animation_data.action:
        return False

    action = arm.animation_data.action
    fcs = _get_action_fcurves(action)

    root_bone_name = None
    for bone in arm.pose.bones:
        if bone.parent is None:
            root_bone_name = bone.name
            break

    to_remove = []
    for fc in fcs:
        if fc.data_path == 'location' and fc.array_index in (0, 1):
            to_remove.append(fc)
            continue
        if root_bone_name:
            target = f'pose.bones["{root_bone_name}"].location'
            if fc.data_path == target and fc.array_index in (0, 1, 2):
                to_remove.append(fc)

    for fc in to_remove:
        try:
            action.fcurves.remove(fc)
        except Exception:
            try:
                for layer in action.layers:
                    for strip in layer.strips:
                        for slot in action.slots:
                            try:
                                cb = strip.channelbag(slot)
                                if cb and fc in list(cb.fcurves):
                                    cb.fcurves.remove(fc)
                            except Exception:
                                pass
            except Exception:
                pass

    return len(to_remove) > 0


def _center_armature_at_origin(arm, frame_start):
    bpy.context.scene.frame_set(frame_start)
    bpy.context.view_layer.update()

    meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o.find_armature() == arm]
    if not meshes:
        meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o.parent == arm]
    if not meshes:
        return

    min_x = min_y =  float('inf')
    max_x = max_y = -float('inf')
    for obj in meshes:
        for corner in obj.bound_box:
            wc = obj.matrix_world @ Vector(corner)
            min_x = min(min_x, wc.x); max_x = max(max_x, wc.x)
            min_y = min(min_y, wc.y); max_y = max(max_y, wc.y)

    if min_x == float('inf'):
        return

    arm.location.x -= (min_x + max_x) / 2
    arm.location.y -= (min_y + max_y) / 2
    bpy.context.view_layer.update()


def _evaluated_mesh_world_center_xy(armature_obj, frame):
    """Centre visuel réel (X,Y world) du mesh animé, modifiers + armature évalués.
    C'est autour de ce point que le rig caméra doit pivoter pour que le personnage
    reste centré dans le cadre quelle que soit la direction."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()

    meshes = [o for o in bpy.data.objects
              if o.type == 'MESH' and not o.hide_render and o.find_armature() == armature_obj]
    if not meshes:
        meshes = [o for o in bpy.data.objects
                  if o.type == 'MESH' and not o.hide_render and o.parent == armature_obj]
    if not meshes:
        return None

    min_x = min_y =  float('inf')
    max_x = max_y = -float('inf')
    for obj in meshes:
        ev = obj.evaluated_get(deps)
        for corner in ev.bound_box:
            wc = ev.matrix_world @ Vector(corner)
            min_x = min(min_x, wc.x); max_x = max(max_x, wc.x)
            min_y = min(min_y, wc.y); max_y = max(max_y, wc.y)
    if min_x == float('inf'):
        return None
    return Vector(((min_x + max_x) / 2, (min_y + max_y) / 2))


def render_spin(path, armature_obj, action_name, settings, context, normal_pass=False):
    scene       = context.scene
    frame_start = settings.frame_start
    frame_end   = settings.frame_end
    frame_step  = get_effective_step(settings)
    num_dir     = settings.num_directions
    fmt         = settings.export_format
    sprite_size = int(settings.sprite_size)
    action_name = sanitize_name(action_name)
    suffix      = "_normal" if normal_pass else ""

    _set_pixel_cfg(settings)

    _apply_render_settings(settings, context)

    rotate_circle = _get_cam_rig()
    if not rotate_circle:
        print(f"[WARN] Pivot caméra '{CAM_RIG_NAME}' introuvable.")
        return

    step_rotation = 360 / num_dir
    action_dir = os.path.join(path, action_name)
    os.makedirs(action_dir, exist_ok=True)

    if normal_pass and settings.export_normals:
        normal_mat = create_normal_map_material()
        originals  = assign_material_to_all_meshes(normal_mat)
    elif settings.flat_colors:
        # Garantit que les matériaux (y compris ajoutés après le toggle) sont mats
        _set_flat_colors(True)

    # Centre le pivot du rig sur le centre VISUEL réel du mesh (pas l'origine objet
    # de l'armature). Ainsi le personnage reste centré dans le cadre quand la caméra
    # orbite → plus de dérive ni de rognage selon la direction.
    saved_rig_loc = rotate_circle.location.copy()
    if armature_obj:
        center = _evaluated_mesh_world_center_xy(armature_obj, frame_start)
        if center is not None:
            rotate_circle.location.x = center.x
            rotate_circle.location.y = center.y
            bpy.context.view_layer.update()

    # Marge de sécurité : dézoom temporaire de la caméra pour ne jamais rogner le
    # personnage. L'autocrop retire ensuite l'espace vide ajouté.
    cam_obj = scene.camera
    saved_ortho = None
    if cam_obj and cam_obj.data and cam_obj.data.type == 'ORTHO':
        saved_ortho = cam_obj.data.ortho_scale
        cam_obj.data.ortho_scale = saved_ortho * RENDER_SAFETY_ZOOM

    tmp_dir = tempfile.mkdtemp()

    if fmt == "Single":
        # Phase 1 : rendu toutes directions
        frames_per_dir = {}
        for x in range(num_dir):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            frame_imgs = {}
            for frame in range(frame_start, frame_end, frame_step):
                scene.frame_set(frame)
                fp = os.path.join(tmp_dir, f"{x}_{frame}.png")
                scene.render.filepath = fp
                bpy.ops.render.render(animation=False, write_still=True)
                frame_imgs[frame] = Image.open(fp).convert("RGBA")
            frames_per_dir[x] = frame_imgs
        # Phase 2 : bbox globale (union sur TOUTES directions + TOUTES frames)
        all_frames_flat = {k: list(v.values()) for k, v in frames_per_dir.items()}
        global_bbox = _get_global_bbox(all_frames_flat)
        # Taille de sortie calculée UNE seule fois → identique pour tous
        out_w, out_h = _bbox_to_output_size(global_bbox, sprite_size) if global_bbox else (sprite_size, sprite_size)
        # Phase 3 : crop + resize exact + save
        for x, frame_imgs in frames_per_dir.items():
            out_dir = os.path.join(action_dir, f"{suffix}dir_{x}" if suffix else f"dir_{x}")
            os.makedirs(out_dir, exist_ok=True)
            for frame, img in sorted(frame_imgs.items()):
                img = _apply_bbox_and_size(img, global_bbox, out_w, out_h)
                img.save(os.path.join(out_dir, f"{frame}.png"))

    elif fmt == "Spritesheet":
        # Phase 1 : rendu toutes directions
        frames_per_dir = {}
        for x in range(num_dir):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            frame_imgs = {}
            for frame in range(frame_start, frame_end, frame_step):
                scene.frame_set(frame)
                fp = os.path.join(tmp_dir, f"{x}_{frame}.png")
                scene.render.filepath = fp
                bpy.ops.render.render(animation=False, write_still=True)
                frame_imgs[frame] = Image.open(fp).convert("RGBA")
            frames_per_dir[x] = frame_imgs
        # Phase 2 : bbox globale → taille cellule calculée UNE fois
        all_frames_flat = {k: list(v.values()) for k, v in frames_per_dir.items()}
        global_bbox = _get_global_bbox(all_frames_flat)
        cell_w, cell_h = _bbox_to_output_size(global_bbox, sprite_size) if global_bbox else (sprite_size, sprite_size)
        num_frames = max(1, math.ceil((frame_end - frame_start) / frame_step))
        sheet = Image.new("RGBA", (cell_w * num_frames, cell_h * num_dir))
        for x, frame_imgs in frames_per_dir.items():
            for fi, (_, img) in enumerate(sorted(frame_imgs.items())):
                cell = _apply_bbox_and_size(img, global_bbox, cell_w, cell_h)
                sheet.paste(cell, (cell_w * fi, cell_h * x))
        sheet.save(os.path.join(action_dir, f"{action_name}{suffix}.png"))

    elif fmt == "GIF_DIR":
        # Phase 1 : rendu toutes directions
        frames_per_dir = {}
        for x in range(num_dir):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            frames = []
            for frame in range(frame_start, frame_end, frame_step):
                scene.frame_set(frame)
                fp = os.path.join(tmp_dir, f"{x}_{frame}.png")
                scene.render.filepath = fp
                bpy.ops.render.render(animation=False, write_still=True)
                frames.append(Image.open(fp).convert("RGBA"))
            frames_per_dir[x] = frames
        # Phase 2 : bbox globale (union sur TOUTES directions + TOUTES frames)
        global_bbox = _get_global_bbox(frames_per_dir)
        # Taille de sortie calculée UNE seule fois → canvas identique pour tous les GIFs
        out_w, out_h = _bbox_to_output_size(global_bbox, sprite_size) if global_bbox else (sprite_size, sprite_size)
        # Phase 3 : crop + resize exact + save GIF par direction
        for x, frames in frames_per_dir.items():
            processed = [_apply_bbox_and_size(f, global_bbox, out_w, out_h) for f in frames]
            save_gif(
                processed,
                os.path.join(action_dir, f"{action_name}{suffix}_{x}.gif"),
                transparent=settings.transparent_bg,
                duration=settings.gif_duration,
            )

    elif fmt == "GIF_ONE":
        # Phase 1 : rendu toutes directions
        frames_per_dir = {}
        for x in range(num_dir):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            frames = []
            for frame in range(frame_start, frame_end, frame_step):
                scene.frame_set(frame)
                fp = os.path.join(tmp_dir, f"{x}_{frame}.png")
                scene.render.filepath = fp
                bpy.ops.render.render(animation=False, write_still=True)
                frames.append(Image.open(fp).convert("RGBA"))
            frames_per_dir[x] = frames
        # Phase 2 : bbox globale → taille calculée UNE fois
        global_bbox = _get_global_bbox(frames_per_dir)
        out_w, out_h = _bbox_to_output_size(global_bbox, sprite_size) if global_bbox else (sprite_size, sprite_size)
        # Phase 3 : crop + resize exact + GIF unique
        all_frames = []
        for x in range(num_dir):
            for f in frames_per_dir[x]:
                all_frames.append(_apply_bbox_and_size(f, global_bbox, out_w, out_h))
        save_gif(
            all_frames,
            os.path.join(action_dir, f"{action_name}{suffix}_combined.gif"),
            transparent=settings.transparent_bg,
            duration=settings.gif_duration,
        )

    rotate_circle.rotation_euler[2] = 0
    rotate_circle.location = saved_rig_loc
    if saved_ortho is not None:
        cam_obj.data.ortho_scale = saved_ortho
    bpy.context.view_layer.update()
    if normal_pass and settings.export_normals:
        restore_materials(originals)


def _character_export_jobs(context):
    """Personnages à exporter = armatures de la collection SpriteForge Characters
    (source de vérité), chacune avec son action et sa provenance. Job =
    (display_name, action, armature, source_dir). Exclut les actions orphelines du
    .blend non rattachées à un personnage importé."""
    jobs = []
    seen = set()
    for arm in _character_armatures():
        action = arm.animation_data.action if arm.animation_data else None
        if action is None or action.name in seen:
            continue
        seen.add(action.name)
        disp = (arm.get("sf_display_name", "") or action.get("sf_display_name", "")
                or _display_name_for_action(context, action.name))
        sdir = (arm.get("sf_source_dir", "") or action.get("sf_source_dir", "")
                or _source_dir_for_action(context, action, arm))
        jobs.append((disp, action, arm, sdir))
    return jobs


def render_all_actions(path, settings, context):
    all_arms = _all_armatures()
    for display_name, action, arm, sdir in _character_export_jobs(context):
        settings.frame_start = int(action.frame_range[0])
        settings.frame_end   = int(action.frame_range[1])
        folder_name = sanitize_name(display_name)
        # <import>/<sous-dossier>/Render/ si demandé, sinon <base>/Render/.
        target = _element_render_dir(settings, sdir, path)
        os.makedirs(target, exist_ok=True)
        restore = _set_render_visibility(arm, all_arms)
        arm.animation_data.action = action
        bpy.context.view_layer.update()
        render_spin(target, arm, folder_name, settings, context, normal_pass=False)
        if settings.export_normals:
            render_spin(target, arm, folder_name, settings, context, normal_pass=True)
        _restore_render_visibility(restore)


def render_single_action(path, settings, action_name, context):
    all_arms = _all_armatures()
    action   = bpy.data.actions.get(action_name)
    if not action:
        print(f"[ERROR] Action introuvable : {action_name}")
        return
    arm = _find_armature_for_action(action)
    if arm is None:
        print("[ERROR] Aucune armature pour cette action.")
        return
    folder_name = sanitize_name(_display_name_for_action(context, action_name))
    # <import>/<sous-dossier>/Render/ si demandé, sinon <base>/Render/.
    sdir = _source_dir_for_action(context, action, arm)
    target = _element_render_dir(settings, sdir, path)
    os.makedirs(target, exist_ok=True)
    restore = _set_render_visibility(arm, all_arms)
    arm.animation_data.action = action
    bpy.context.view_layer.update()
    render_spin(target, arm, folder_name, settings, context, normal_pass=False)
    if settings.export_normals:
        render_spin(target, arm, folder_name, settings, context, normal_pass=True)
    _restore_render_visibility(restore)


# ══════════════════════════════════════════════════════════════════════════════
#  RENDER ASSETS
# ══════════════════════════════════════════════════════════════════════════════

def _import_asset_file(path, context=None):
    before = set(bpy.data.objects.keys())
    ext    = os.path.splitext(path)[1].lower()

    screen  = (context or bpy.context).screen
    area_3d = next((a for a in screen.areas if a.type == 'VIEW_3D'), None)

    def _do_import():
        if ext == '.fbx':
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext == '.obj':
            bpy.ops.wm.obj_import(filepath=path)
        elif ext in ('.glb', '.gltf'):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == '.dae':
            bpy.ops.wm.collada_import(filepath=path)
        else:
            raise ValueError(f"Format non supporté : {ext}")

    try:
        if area_3d:
            with bpy.context.temp_override(area=area_3d):
                _do_import()
        else:
            _do_import()
    except Exception as e:
        print(f"[ERROR] Import {path} : {e}")
        return []

    return list(set(bpy.data.objects.keys()) - before)

def _move_asset_to_origin(obj_names):
    bpy.context.view_layer.update()
    min_co = Vector((float('inf'),)  * 3)
    max_co = Vector((float('-inf'),) * 3)
    obj_names_set = set(obj_names)

    for name in obj_names:
        obj = bpy.data.objects.get(name)
        if obj and obj.type == 'MESH':
            for corner in obj.bound_box:
                wc = obj.matrix_world @ Vector(corner)
                for i in range(3):
                    min_co[i] = min(min_co[i], wc[i])
                    max_co[i] = max(max_co[i], wc[i])

    if min_co[0] == float('inf'):
        return

    offset = Vector((
        -((min_co[0] + max_co[0]) / 2),
        -((min_co[1] + max_co[1]) / 2),
        -min_co[2],
    ))

    for name in obj_names:
        obj = bpy.data.objects.get(name)
        if obj and (obj.parent is None or obj.parent.name not in obj_names_set):
            obj.location += offset

    bpy.context.view_layer.update()

def render_asset(asset_name, asset_path, settings, output_path, context):
    scene = context.scene
    sprite_size = int(settings.sprite_size)

    _set_pixel_cfg(settings)

    existing_names = set(bpy.data.objects.keys())
    render_hide_state = {}
    for name in existing_names:
        obj = bpy.data.objects.get(name)
        if obj and obj.type not in ('CAMERA', 'LIGHT', 'EMPTY'):
            render_hide_state[name] = obj.hide_render
            obj.hide_render = True

    new_names = _import_asset_file(asset_path, context)
    if not new_names:
        for name, val in render_hide_state.items():
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_render = val
        return

    rig = _get_cam_rig()
    if rig:
        rig.location = Vector((0.0, 0.0, 0.0))
    _move_asset_to_origin(new_names)

    # Applique le mode couleurs mates aux matériaux fraîchement importés
    if settings.flat_colors:
        _set_flat_colors(True)

    # Rendu interne haute résolution
    internal = sprite_size * INTERNAL_RENDER_MULT
    scene.render.resolution_x    = internal
    scene.render.resolution_y    = internal
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode  = 'RGBA'
    if settings.pixel_art_enabled:
        # filter_size : 0 = pixels nets, 1.5 = bords lissés (selon l'anti-aliasing).
        scene.render.filter_size = 1.5 if settings.pixel_antialias else 0.0
        try:
            scene.view_settings.view_transform = 'Standard'
        except Exception:
            pass
    try:
        scene.eevee.use_shadows = settings.cast_shadows
    except Exception:
        pass

    scene.frame_set(1)
    bpy.context.view_layer.update()

    if not rig:
        _cleanup_asset_objects(new_names)
        return

    # Marge de sécurité : dézoom temporaire pour ne jamais rogner l'asset.
    cam_obj = scene.camera
    saved_ortho = None
    if cam_obj and cam_obj.data and cam_obj.data.type == 'ORTHO':
        saved_ortho = cam_obj.data.ortho_scale
        cam_obj.data.ortho_scale = saved_ortho * RENDER_SAFETY_ZOOM

    num_dir       = settings.num_directions
    step_rotation = 360 / num_dir
    selected      = _selected_dir_indices(settings.selected_dirs_mask, num_dir)
    if not selected:
        selected = list(range(num_dir))

    sname   = sanitize_name(asset_name)
    tmp_dir = tempfile.mkdtemp()

    # Phase 1 : rendu toutes directions sélectionnées
    frames_per_dir = {}
    for x in selected:
        rig.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
        bpy.context.view_layer.update()
        fp = os.path.join(tmp_dir, f"{x}.png")
        scene.render.filepath = fp
        bpy.ops.render.render(animation=False, write_still=True)
        frames_per_dir[x] = [Image.open(fp).convert("RGBA")]

    # Phase 2 : bbox globale (union sur toutes les directions sélectionnées)
    global_bbox = _get_global_bbox(frames_per_dir)
    # Taille calculée UNE fois → identique pour tous les angles
    out_w, out_h = _bbox_to_output_size(global_bbox, sprite_size) if global_bbox else (sprite_size, sprite_size)

    # Phase 3 : crop + resize exact + save
    multi_dir = len(selected) > 1
    for x, (img,) in [(k, v) for k, v in frames_per_dir.items()]:
        img = _apply_bbox_and_size(img, global_bbox, out_w, out_h)
        if multi_dir:
            lbl     = _dir_label(x, num_dir)
            dir_out = os.path.join(output_path, lbl)
            os.makedirs(dir_out, exist_ok=True)
            img.save(os.path.join(dir_out, f"{sname}.png"))
        else:
            os.makedirs(output_path, exist_ok=True)
            img.save(os.path.join(output_path, f"{sname}.png"))

    rig.rotation_euler[2] = 0
    if saved_ortho is not None:
        cam_obj.data.ortho_scale = saved_ortho
    _cleanup_asset_objects(new_names)

    for name, val in render_hide_state.items():
        obj = bpy.data.objects.get(name)
        if obj:
            obj.hide_render = val


def _cleanup_asset_objects(new_names):
    for name in list(new_names):
        obj = bpy.data.objects.get(name)
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)
    for blk_col, fn in [
        (bpy.data.meshes,    bpy.data.meshes.remove),
        (bpy.data.armatures, bpy.data.armatures.remove),
        (bpy.data.materials, bpy.data.materials.remove),
        (bpy.data.images,    bpy.data.images.remove),
    ]:
        for blk in list(blk_col):
            if blk.users == 0:
                fn(blk)


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS — IMPORT PERSONNAGES
# ══════════════════════════════════════════════════════════════════════════════

class OMNI_OT_scan_fbx(Operator):
    bl_idname      = "omni.scan_fbx"
    bl_label       = "Scanner"
    bl_description = "Détecte les animations disponibles dans le dossier"

    def execute(self, context):
        props = context.scene.omni_render_settings
        if not _populate_anim_items(context):
            self.report({'ERROR'}, "Dossier invalide.")
            return {'CANCELLED'}
        self.report({'INFO'}, props.import_status)
        return {'FINISHED'}


class OMNI_OT_remove_anim_item(Operator):
    bl_idname = "omni.remove_anim_item"
    bl_label  = "Retirer"
    index: IntProperty()

    def execute(self, context):
        items = context.scene.omni_anim_items
        if 0 <= self.index < len(items):
            items.remove(self.index)
        return {'FINISHED'}


class OMNI_OT_import_selected(Operator):
    bl_idname      = "omni.import_selected"
    bl_label       = "Valider l'import"
    bl_description = "Importe les animations listées puis sauvegarde le .blend"

    def execute(self, context):
        props = context.scene.omni_render_settings
        items = context.scene.omni_anim_items
        if not items:
            self.report({'ERROR'}, "Lance d'abord un scan.")
            return {'CANCELLED'}

        imported_count = 0
        for item in items:
            if not os.path.isfile(item.fbx_path):
                self.report({'WARNING'}, f"FBX introuvable : {item.fbx_path}")
                continue
            new_objs = _do_import_fbx(item.fbx_path)
            if item.is_merged:
                items.clear()
                all_arms = [bpy.data.objects[n] for n in new_objs
                            if bpy.data.objects.get(n) and bpy.data.objects[n].type == 'ARMATURE']
                for arm in all_arms:
                    _make_animation_inplace(arm)
                    action = arm.animation_data.action if arm.animation_data else None
                    it = items.add()
                    it.display_name = action.name if action else arm.name
                    it.arm_name     = arm.name
                    it.action_name  = action.name if action else ""
                    it.is_merged    = True
                    it.imported     = True
                    # Persiste le nom affichable (FBX fusionné → pas de sous-dossier)
                    if action:
                        action["sf_display_name"] = it.display_name
                    arm["sf_display_name"] = it.display_name
                imported_count += len(all_arms)
                break
            else:
                arm = _find_armature_in(new_objs)
                if arm:
                    _make_animation_inplace(arm)
                    frame_s = int(arm.animation_data.action.frame_range[0]) \
                              if arm.animation_data and arm.animation_data.action else 1
                    _center_armature_at_origin(arm, frame_s)
                    if arm.animation_data and arm.animation_data.action:
                        item.arm_name    = arm.name
                        item.action_name = arm.animation_data.action.name
                        # Persiste nom du FBX + sous-dossier de provenance pour que le
                        # menu d'export et le routage survivent au vidage de la liste.
                        act = arm.animation_data.action
                        act["sf_display_name"] = item.display_name
                        act["sf_source_dir"]   = item.source_dir
                        arm["sf_display_name"] = item.display_name
                        arm["sf_source_dir"]   = item.source_dir
                item.imported = True
                imported_count += 1

        if not props.output_path:
            props.output_path = props.input_dir

        # Applique couleurs mates aux matériaux fraîchement importés
        if props.flat_colors:
            _set_flat_colors(True)

        props.import_status = f"{imported_count} animation(s) importée(s)"

        first_action = next(iter(bpy.data.actions), None)
        if first_action:
            props.frame_start = int(first_action.frame_range[0])
            props.frame_end   = int(first_action.frame_range[1])

        # Ajoute tous les nouveaux objets (armatures + meshes) à SpriteForge Characters
        _ensure_spriteforge_collections()
        col = bpy.data.collections.get(COL_CHARACTERS)
        if col:
            for obj in list(bpy.context.scene.objects):
                if obj.type in ('ARMATURE', 'MESH') and obj.name not in [
                    o.name for o in col.all_objects
                ]:
                    already_in_sf = any(
                        obj.name in [o.name for o in c.all_objects]
                        for c in [bpy.data.collections.get(COL_ASSETS),
                                  bpy.data.collections.get(COL_CAMERA)]
                        if c
                    )
                    if not already_in_sf:
                        for c in list(obj.users_collection):
                            c.objects.unlink(obj)
                        col.objects.link(obj)

        # Vide la liste de scan après import validé
        items.clear()

        if bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
            props.import_status += " · .blend sauvegardé"
        self.report({'INFO'}, props.import_status)
        return {'FINISHED'}


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS — ASSETS
# ══════════════════════════════════════════════════════════════════════════════

class OMNI_OT_scan_assets(Operator):
    bl_idname      = "omni.scan_assets"
    bl_label       = "Scanner"
    bl_description = "Scanne le dossier pour lister les fichiers 3D"

    def execute(self, context):
        props = context.scene.omni_render_settings
        if not _populate_asset_items(context):
            self.report({'ERROR'}, "Dossier invalide.")
            return {'CANCELLED'}
        self.report({'INFO'}, props.assets_status)
        return {'FINISHED'}


class OMNI_OT_remove_asset_item(Operator):
    bl_idname = "omni.remove_asset_item"
    bl_label  = "Retirer"
    index: IntProperty()

    def execute(self, context):
        items = context.scene.omni_asset_items
        if 0 <= self.index < len(items):
            items.remove(self.index)
        return {'FINISHED'}


class OMNI_OT_import_assets(Operator):
    bl_idname      = "omni.import_assets"
    bl_label       = "Valider l'import"
    bl_description = "Importe les assets listés dans la scène puis sauvegarde le .blend"

    def execute(self, context):
        props = context.scene.omni_render_settings
        items = context.scene.omni_asset_items
        if not items:
            self.report({'ERROR'}, "Lance d'abord un scan.")
            return {'CANCELLED'}

        imported_count = 0
        _ensure_spriteforge_collections()
        for item in items:
            if item.imported:
                continue
            if not os.path.isfile(item.file_path):
                self.report({'WARNING'}, f"Fichier introuvable : {item.file_path}")
                continue
            new_names = _import_asset_file(item.file_path, context)
            if new_names:
                # Stocke nom/chemin sur les racines pour export sans relire la liste
                for n in new_names:
                    obj = bpy.data.objects.get(n)
                    if obj and obj.parent is None:
                        obj["sf_asset_name"] = item.display_name
                        obj["sf_asset_path"] = item.file_path
                        obj["sf_source_dir"] = item.source_dir
                _add_to_collection(new_names, COL_ASSETS)
                # Applique couleurs mates aux matériaux fraîchement importés
                if props.flat_colors:
                    _set_flat_colors(True)
                item.imported = True
                imported_count += 1
            else:
                self.report({'WARNING'}, f"Import échoué : {item.display_name}")

        if not props.output_path and props.assets_dir:
            props.output_path = props.assets_dir

        props.assets_status = f"{imported_count} asset(s) importé(s)"

        # Vide la liste de scan après import validé
        items.clear()

        if bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
            props.assets_status += " · .blend sauvegardé"

        self.report({'INFO'}, props.assets_status)
        return {'FINISHED'}


class OMNI_OT_clear_anim_list(Operator):
    bl_idname      = "omni.clear_anim_list"
    bl_label       = "Vider la liste"
    bl_description = "Supprime tous les personnages / animations de la liste"

    def execute(self, context):
        context.scene.omni_anim_items.clear()
        context.scene.omni_render_settings.import_status = ""
        return {'FINISHED'}


class OMNI_OT_clear_asset_list(Operator):
    bl_idname      = "omni.clear_asset_list"
    bl_label       = "Vider la liste"
    bl_description = "Supprime tous les assets de la liste"

    def execute(self, context):
        context.scene.omni_asset_items.clear()
        context.scene.omni_render_settings.assets_status = ""
        return {'FINISHED'}


class OMNI_OT_toggle_dir(Operator):
    bl_idname      = "omni.toggle_dir"
    bl_label       = "Basculer direction"
    bl_description = "Sélectionne/désélectionne cette direction"
    index: IntProperty()

    def execute(self, context):
        props = context.scene.omni_render_settings
        props.selected_dirs_mask ^= (1 << self.index)
        return {'FINISHED'}


class OMNI_OT_select_all_dirs(Operator):
    bl_idname      = "omni.select_all_dirs"
    bl_label       = "Tout"
    bl_description = "Sélectionne toutes les directions"

    def execute(self, context):
        props = context.scene.omni_render_settings
        props.selected_dirs_mask = (1 << props.num_directions) - 1
        return {'FINISHED'}


class OMNI_OT_deselect_all_dirs(Operator):
    bl_idname      = "omni.deselect_all_dirs"
    bl_label       = "Aucune"
    bl_description = "Désélectionne toutes les directions"

    def execute(self, context):
        context.scene.omni_render_settings.selected_dirs_mask = 0
        return {'FINISHED'}


def _check_ready_for_export(operator, context, items, kind):
    """Garde commune aux exports : vérifie que la collection est non-vide et que
    la caméra existe. *kind* = 'asset' ou 'perso'."""
    col_name = COL_ASSETS if kind == 'asset' else COL_CHARACTERS
    col_ok = _collection_has_objects(col_name)
    # Fallback : ancienne vérification sur items (si collection absente)
    if not col_ok:
        col_ok = bool(items and any(getattr(it, "imported", False) for it in items))
    if not col_ok:
        noun = "les assets" if kind == 'asset' else "les personnages"
        operator.report({'ERROR'}, f"Validez d'abord l'importation ({noun}) avant d'exporter.")
        return False
    if not _camera_rig_exists():
        operator.report({'ERROR'}, "Créez d'abord la caméra iso (section Caméra).")
        return False
    return True


class OMNI_OT_export_single_asset(Operator):
    bl_idname      = "omni.export_single_asset"
    bl_label       = "Exporter cet asset"
    bl_description = "Rend uniquement l'asset sélectionné"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        items    = context.scene.omni_asset_items
        if not _check_ready_for_export(self, context, items, 'asset'):
            return {'CANCELLED'}

        sel = settings.selected_asset_idx  # nom d'objet (collection) ou index (fallback)
        asset_name = asset_path = asset_sdir = ""

        # Priorité : collection SpriteForge Assets
        col = bpy.data.collections.get(COL_ASSETS)
        if col:
            obj = col.all_objects.get(sel) if sel else None
            if obj:
                asset_name = obj.get("sf_asset_name", obj.name)
                asset_path = obj.get("sf_asset_path", "")
                asset_sdir = obj.get("sf_source_dir", "")

        # Fallback : ancienne liste omni_asset_items
        if not asset_name:
            try:
                idx = int(sel)
            except (ValueError, TypeError):
                idx = -1
            if idx < 0 or idx >= len(items):
                self.report({'ERROR'}, "Aucun asset sélectionné.")
                return {'CANCELLED'}
            item = items[idx]
            asset_name = item.display_name
            asset_path = item.file_path
            asset_sdir = item.source_dir

        if not asset_path or not os.path.isfile(asset_path):
            self.report({'ERROR'}, f"Fichier introuvable : {asset_path}")
            return {'CANCELLED'}

        base_path = _get_base_path(settings)
        # <import>/<sous-dossier>/Render/ si demandé, sinon <base>/Render/.
        target = _element_render_dir(settings, asset_sdir, base_path)
        try:
            render_asset(asset_name, asset_path, settings, target, context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Export → {target}")
        return {'FINISHED'}


class OMNI_OT_export_assets(Operator):
    bl_idname      = "omni.export_assets"
    bl_label       = "Exporter tous les assets"
    bl_description = "Rend tous les assets de la collection SpriteForge Assets"

    def execute(self, context):
        settings  = context.scene.omni_render_settings
        items     = context.scene.omni_asset_items
        if not _check_ready_for_export(self, context, items, 'asset'):
            return {'CANCELLED'}
        base_path = _get_base_path(settings)

        def _target(sdir):
            # <import>/<sous-dossier>/Render/ si demandé, sinon <base>/Render/.
            return _element_render_dir(settings, sdir, base_path)

        # Priorité : collection SpriteForge Assets
        col = bpy.data.collections.get(COL_ASSETS)
        # IMPORTANT : on fige la liste des assets AVANT de rendre quoi que ce soit.
        # render_asset() importe puis supprime des objets ; itérer directement sur
        # col.all_objects (collection RNA calculée à la volée) pendant ces
        # modifications invalide l'itérateur et fait crasher Blender (access violation).
        assets_to_render = []
        if col:
            seen_names = set()
            for obj in col.all_objects:
                aname = obj.get("sf_asset_name", "")
                apath = obj.get("sf_asset_path", "")
                sdir  = obj.get("sf_source_dir", "")
                if not aname or aname in seen_names:
                    continue
                seen_names.add(aname)
                assets_to_render.append((aname, apath, sdir))

        if assets_to_render:
            for aname, apath, sdir in assets_to_render:
                if not apath or not os.path.isfile(apath):
                    self.report({'WARNING'}, f"Chemin introuvable pour : {aname}")
                    continue
                try:
                    render_asset(aname, apath, settings, _target(sdir), context)
                except Exception as e:
                    self.report({'ERROR'}, f"{aname} : {e}")
        else:
            # Fallback : ancienne liste
            for item in items:
                if not os.path.isfile(item.file_path):
                    self.report({'WARNING'}, f"Fichier introuvable : {item.file_path}")
                    continue
                try:
                    render_asset(item.display_name, item.file_path, settings,
                                 _target(item.source_dir), context)
                except Exception as e:
                    self.report({'ERROR'}, f"{item.display_name} : {e}")

        dest = "sous-dossiers d'origine" if settings.export_to_subdirs and _export_subdirs(context, False) \
               else os.path.join(base_path, "Render")
        self.report({'INFO'}, f"Export assets → {dest}")
        return {'FINISHED'}


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS — CAMÉRA
# ══════════════════════════════════════════════════════════════════════════════

class OMNI_OT_setup_camera(Operator):
    bl_idname      = "omni.setup_camera"
    bl_label       = "Créer la caméra iso"
    bl_description = "Crée le pivot CortexCamRig avec la caméra iso et les lumières"

    def execute(self, context):
        props = context.scene.omni_render_settings
        is_assets = (props.export_mode == 'ASSETS')

        _ensure_spriteforge_collections()
        cam_col = bpy.data.collections.get(COL_CAMERA)

        def _link_to_cam_col(obj):
            if cam_col:
                cam_col.objects.link(obj)
            else:
                bpy.context.collection.objects.link(obj)

        empty = bpy.data.objects.new(CAM_RIG_NAME, None)
        _link_to_cam_col(empty)
        empty.empty_display_type = 'ARROWS'

        cam_data = bpy.data.cameras.new("SpriteForge_Cam")
        cam_obj  = bpy.data.objects.new("SpriteForge_Cam", cam_data)
        cam_obj.parent = empty
        _link_to_cam_col(cam_obj)
        cam_data.type        = 'ORTHO'
        cam_data.ortho_scale = props.cam_view_width
        cam_obj.location     = (0, -14.82, 18.63)
        cam_obj.rotation_euler = [math.radians(38.6), 0, 0]
        context.scene.camera = cam_obj

        light_data        = bpy.data.lights.new("SpriteForge_Light", type='POINT')
        light_data.energy = props.main_light_energy
        light             = bpy.data.objects.new("SpriteForge_Light", light_data)
        light.parent      = empty
        _link_to_cam_col(light)
        light.location = (0, -4, 4)

        if is_assets:
            spot_data = bpy.data.lights.new(SPOT_TOP_NAME, type='SPOT')
            spot      = bpy.data.objects.new(SPOT_TOP_NAME, spot_data)
            spot.parent = empty
            _link_to_cam_col(spot)
            _configure_top_spot(spot_data, spot, props.spot_light_energy)

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.region_3d.view_perspective = 'CAMERA'
                        break
        return {'FINISHED'}


class OMNI_OT_add_asset_light(Operator):
    bl_idname      = "omni.add_asset_light"
    bl_label       = "Ajouter lumière asset"
    bl_description = "Ajoute le spot supérieur pour l'éclairage des assets"

    def execute(self, context):
        rig = _get_cam_rig()
        if not rig:
            self.report({'ERROR'}, "Créez d'abord le rig caméra.")
            return {'CANCELLED'}
        existing = bpy.data.objects.get(SPOT_TOP_NAME)
        if existing:
            existing.hide_viewport = False
            existing.hide_render   = False
            self.report({'INFO'}, "Spot supérieur réactivé.")
            return {'FINISHED'}
        spot_data = bpy.data.lights.new(SPOT_TOP_NAME, type='SPOT')
        spot      = bpy.data.objects.new(SPOT_TOP_NAME, spot_data)
        spot.parent = rig
        bpy.context.collection.objects.link(spot)
        _configure_top_spot(spot_data, spot, context.scene.omni_render_settings.spot_light_energy)
        self.report({'INFO'}, "Spot supérieur créé.")
        return {'FINISHED'}


class OMNI_OT_cam_zoom(Operator):
    bl_idname      = "omni.cam_zoom"
    bl_label       = "Zoom caméra"
    bl_description = "Ajuste l'ortho scale (largeur BU)"
    delta: FloatProperty(default=0.5)

    def execute(self, context):
        cam   = context.scene.camera
        props = context.scene.omni_render_settings
        if cam and cam.data and cam.data.type == 'ORTHO':
            new_w = max(0.1, cam.data.ortho_scale + self.delta)
            cam.data.ortho_scale = new_w
            _sync_cam_size_props(context)
        return {'FINISHED'}


class OMNI_OT_cam_pan(Operator):
    bl_idname      = "omni.cam_pan"
    bl_label       = "Pan caméra"
    bl_description = "Déplace le pivot caméra"
    axis:  StringProperty(default='X')
    delta: FloatProperty(default=0.5)

    def execute(self, context):
        empty = _get_cam_rig()
        if not empty:
            self.report({'WARNING'}, f"Pivot caméra '{CAM_RIG_NAME}' introuvable.")
            return {'CANCELLED'}
        if self.axis == 'X':
            empty.location.x += self.delta
        elif self.axis == 'Y':
            empty.location.y += self.delta
        elif self.axis == 'Z':
            empty.location.z += self.delta
        return {'FINISHED'}


class OMNI_OT_cam_to_view(Operator):
    bl_idname      = "omni.cam_to_view"
    bl_label       = "Vue caméra"
    bl_description = "Bascule le viewport en vue caméra"

    def execute(self, context):
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.region_3d.view_perspective = 'CAMERA'
                        break
        return {'FINISHED'}


class OMNI_OT_pixel_refresh_view(Operator):
    bl_idname      = "omni.pixel_refresh_view"
    bl_label       = "Recadrer la vue caméra"
    bl_description = ("Réapplique la grille pixel sur la caméra et bascule le viewport "
                      "en vue caméra. (La pixelisation elle-même se voit à l'aperçu / "
                      "à l'export, pas dans le viewport.)")

    def execute(self, context):
        _apply_pixel_art_state(context.scene, context)
        props = context.scene.omni_render_settings
        if props.pixel_art_enabled:
            self.report({'INFO'}, f"Vue caméra · grille {int(props.pixel_resolution)}²")
        else:
            self.report({'INFO'}, "Pixel art désactivé.")
        return {'FINISHED'}


def _render_pixel_preview(context):
    """Rend la caméra courante, applique le pipeline pixel art (réduction grille +
    dither + contour) et renvoie le chemin du PNG d'aperçu généré."""
    scene    = context.scene
    settings = scene.omni_render_settings
    _set_pixel_cfg(settings)

    # Sauvegarde des réglages de rendu modifiés
    r = scene.render
    saved = (r.resolution_x, r.resolution_y, r.resolution_percentage, r.film_transparent,
             r.image_settings.file_format, r.image_settings.color_mode, r.filepath)

    internal = int(settings.sprite_size) * INTERNAL_RENDER_MULT
    r.resolution_x = internal
    r.resolution_y = internal
    r.resolution_percentage = 100
    r.film_transparent = settings.transparent_bg
    r.image_settings.file_format = 'PNG'
    r.image_settings.color_mode  = 'RGBA' if settings.transparent_bg else 'RGB'

    src = os.path.join(tempfile.gettempdir(), "sf_pixel_preview_src.png")
    r.filepath = src
    bpy.ops.render.render(write_still=True)

    img  = Image.open(src).convert("RGBA")
    bbox = img.split()[3].getbbox()
    size = int(settings.sprite_size)
    out_w, out_h = _bbox_to_output_size(bbox, size) if bbox else (size, size)
    img = _apply_bbox_and_size(img, bbox, out_w, out_h)

    out_path = os.path.join(tempfile.gettempdir(), "sf_pixel_preview.png")
    img.save(out_path)

    # Restaure les réglages de rendu
    (r.resolution_x, r.resolution_y, r.resolution_percentage, r.film_transparent,
     r.image_settings.file_format, r.image_settings.color_mode, r.filepath) = saved
    return out_path


class OMNI_OT_pixel_preview(Operator):
    bl_idname      = "omni.pixel_preview"
    bl_label       = "Aperçu pixel art"
    bl_description = ("Rend un aperçu de la caméra avec le rendu pixel art complet "
                      "(résolution, contour, dithering, anti-aliasing) et l'affiche")

    def execute(self, context):
        if not _camera_rig_exists():
            self.report({'ERROR'}, "Créez d'abord la caméra iso (section Caméra).")
            return {'CANCELLED'}
        try:
            path = _render_pixel_preview(context)
        except Exception as e:
            self.report({'ERROR'}, f"Aperçu échoué : {e}")
            return {'CANCELLED'}

        # Charge/rafraîchit l'image dans Blender
        name = "SF Pixel Art Preview"
        img = bpy.data.images.get(name)
        if img:
            img.filepath = path
            img.reload()
        else:
            img = bpy.data.images.load(path)
            img.name = name

        # Affiche dans un éditeur d'image s'il y en a un, sinon ouvre le PNG
        shown = False
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = img
                area.tag_redraw()
                shown = True
        if not shown:
            try:
                os.startfile(path)  # ouvre dans la visionneuse Windows
            except Exception:
                pass
        self.report({'INFO'}, f"Aperçu pixel art généré : {os.path.basename(path)}")
        return {'FINISHED'}


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS — RENDU / EXPORT PERSONNAGES
# ══════════════════════════════════════════════════════════════════════════════

class OMNI_OT_auto_gif_duration(Operator):
    bl_idname      = "omni.auto_gif_duration"
    bl_label       = "Auto ms"
    bl_description = "Calcule la durée GIF depuis le FPS scène et le step effectif"

    def execute(self, context):
        props = context.scene.omni_render_settings
        fps   = context.scene.render.fps / context.scene.render.fps_base
        step  = get_effective_step(props)
        props.gif_duration = max(1, round((step / fps) * 1000))
        self.report({'INFO'}, f"Durée GIF : {props.gif_duration} ms/frame")
        return {'FINISHED'}


class OMNI_OT_sync_frames(Operator):
    bl_idname      = "omni.sync_frames"
    bl_label       = "Sync frames"
    bl_description = "Récupère Start/End depuis l'action active de l'armature"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            for o in context.scene.objects:
                if o.type == 'ARMATURE' and o.animation_data and o.animation_data.action:
                    obj = o
                    break
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Aucune armature avec action trouvée.")
            return {'CANCELLED'}
        if not obj.animation_data or not obj.animation_data.action:
            self.report({'ERROR'}, "L'armature n'a pas d'action active.")
            return {'CANCELLED'}
        action = obj.animation_data.action
        props  = context.scene.omni_render_settings
        props.frame_start = int(action.frame_range[0])
        props.frame_end   = int(action.frame_range[1])
        self.report({'INFO'}, f"Frames : {props.frame_start} → {props.frame_end}")
        return {'FINISHED'}


class OMNI_OT_export_all(Operator):
    bl_idname = "omni.export_all"
    bl_label  = "Exporter toutes les actions"

    def execute(self, context):
        settings  = context.scene.omni_render_settings
        if not _check_ready_for_export(self, context, context.scene.omni_anim_items, 'perso'):
            return {'CANCELLED'}
        base_path = _get_base_path(settings)
        try:
            render_all_actions(base_path, settings, context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Export → {base_path}")
        return {'FINISHED'}


class OMNI_OT_export_specific(Operator):
    bl_idname = "omni.export_specific"
    bl_label  = "Exporter l'action sélectionnée"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        if not _check_ready_for_export(self, context, context.scene.omni_anim_items, 'perso'):
            return {'CANCELLED'}
        if not settings.selected_action:
            self.report({'ERROR'}, "Aucune action sélectionnée.")
            return {'CANCELLED'}
        action = bpy.data.actions.get(settings.selected_action)
        if action:
            settings.frame_start = int(action.frame_range[0])
            settings.frame_end   = int(action.frame_range[1])
        base_path = _get_base_path(settings)
        try:
            render_single_action(base_path, settings, settings.selected_action, context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Export → {base_path}")
        return {'FINISHED'}


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL
# ══════════════════════════════════════════════════════════════════════════════

class OMNI_PT_panel(Panel):
    bl_label       = "Cortex SpriteForge"
    bl_idname      = "OMNI_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'SpriteForge'

    def draw(self, context):
        layout   = self.layout
        props    = context.scene.omni_render_settings
        has_rig  = _camera_rig_exists()
        is_chars = (props.export_mode == 'CHARACTERS')
        is_assets = (props.export_mode == 'ASSETS')

        # ── MODE ─────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop_enum(props, "export_mode", "CHARACTERS")
        row.prop_enum(props, "export_mode", "ASSETS")

        layout.separator(factor=0.4)

        # ── IMPORT (personnages uniquement) ───────────────────────
        if is_chars:
            box = layout.box()
            box.label(text="Import personnages", icon='IMPORT')
            info = box.row()
            info.enabled = False
            info.label(text=f"Collection : {COL_CHARACTERS}", icon='OUTLINER_COLLECTION')
            row = box.row(align=True)
            op = row.operator("omni.open_folder", text="", icon='FILE_FOLDER')
            op.path = props.input_dir
            row.prop(props, "input_dir", text="")
            row.operator("omni.scan_fbx", text="", icon='VIEWZOOM')

            items = context.scene.omni_anim_items
            if items:
                col = box.column(align=True)
                col.scale_y = 0.9
                current_dir = None
                for i, item in enumerate(items):
                    if item.source_dir != current_dir:
                        current_dir = item.source_dir
                        if current_dir:
                            col.separator(factor=0.4)
                            sub = col.row()
                            sub.enabled = False
                            sub.label(text=current_dir, icon='FILE_FOLDER')
                    r = col.row(align=True)
                    r.label(text=item.display_name,
                            icon='CHECKMARK' if item.imported else 'ANIM')
                    op = r.operator("omni.remove_anim_item", text="", icon='X')
                    op.index = i
                box.separator(factor=0.3)
                row = box.row(align=True)
                row.operator("omni.import_selected", text="Valider l'import", icon='IMPORT')
                row.operator("omni.clear_anim_list", text="", icon='TRASH')
            else:
                box.operator("omni.scan_fbx", text="Scanner le dossier", icon='FILE_FOLDER')

            if props.import_status:
                box.label(text=props.import_status, icon='INFO')

            layout.separator(factor=0.3)

        # ── ASSETS ───────────────────────────────────────────────
        if is_assets:
            box = layout.box()
            box.label(text="Import assets", icon='MESH_DATA')
            info = box.row()
            info.enabled = False
            info.label(text=f"Collection : {COL_ASSETS}", icon='OUTLINER_COLLECTION')
            row = box.row(align=True)
            op = row.operator("omni.open_folder", text="", icon='FILE_FOLDER')
            op.path = props.assets_dir
            row.prop(props, "assets_dir", text="")
            row.operator("omni.scan_assets", text="", icon='VIEWZOOM')

            asset_items = context.scene.omni_asset_items
            if asset_items:
                col = box.column(align=True)
                col.scale_y = 0.9
                current_dir = None
                for i, item in enumerate(asset_items):
                    if item.source_dir != current_dir:
                        current_dir = item.source_dir
                        if current_dir:
                            col.separator(factor=0.4)
                            sub = col.row()
                            sub.enabled = False
                            sub.label(text=current_dir, icon='FILE_FOLDER')
                    r = col.row(align=True)
                    r.label(text=item.display_name,
                            icon='CHECKMARK' if item.imported else 'OBJECT_DATA')
                    op = r.operator("omni.remove_asset_item", text="", icon='X')
                    op.index = i
                box.separator(factor=0.3)
                row = box.row(align=True)
                row.operator("omni.import_assets", text="Valider l'import", icon='IMPORT')
                row.operator("omni.clear_asset_list", text="", icon='TRASH')
            else:
                box.operator("omni.scan_assets", text="Scanner le dossier", icon='FILE_FOLDER')

            if props.assets_status:
                box.label(text=props.assets_status, icon='INFO')

            layout.separator(factor=0.3)

        # ── CAMÉRA ───────────────────────────────────────────────
        box = layout.box()
        box.label(text="Caméra", icon='CAMERA_DATA')

        if not has_rig:
            box.operator("omni.setup_camera", text="Créer la caméra iso", icon='ADD')
        else:
            rig = _get_cam_rig()
            cam = context.scene.camera

            col = box.column(align=True)
            col.scale_y = 0.75
            col.label(text=f"Pivot : {rig.name if rig else '-'}", icon='EMPTY_ARROWS')
            col.label(text=f"Caméra : {cam.name if cam else '-'}", icon='CAMERA_DATA')

            box.separator(factor=0.2)
            box.operator("omni.cam_to_view", text="Vue caméra", icon='VIEW_CAMERA')

            if is_assets:
                spot = bpy.data.objects.get(SPOT_TOP_NAME)
                if spot is None or spot.hide_render:
                    box.operator("omni.add_asset_light", text="Ajouter spot supérieur", icon='LIGHT_SPOT')

            # ── Position pivot (offsets X/Y/Z) ───────────────────
            box.separator(factor=0.3)
            pbox = box.box()
            prow = pbox.row()
            prow.label(text="Position pivot", icon='EMPTY_ARROWS')
            prow.operator("omni.reset_cam_offset", text="", icon='LOOP_BACK')
            col = pbox.column(align=True)
            col.prop(props, "cam_offset_x", slider=True)
            col.prop(props, "cam_offset_y", slider=True)
            col.prop(props, "cam_offset_z", slider=True)

            # ── Champ de vision ───────────────────────────────────
            box.separator(factor=0.2)
            fbox = box.box()
            fbox.label(text="Champ de vision", icon='CAMERA_DATA')
            fcol = fbox.column(align=True)
            row = fcol.row(align=True)
            op = row.operator("omni.cam_zoom", text="", icon='REMOVE'); op.delta = -0.5
            row.prop(props, "cam_view_width", text="Largeur", slider=True)
            op = row.operator("omni.cam_zoom", text="", icon='ADD'); op.delta = +0.5

        layout.separator(factor=0.3)

        # ── RENDU ────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Rendu", icon='RENDER_STILL')

        # Taille sprite — unique dropdown pour les deux modes
        row = box.row(align=True)
        row.label(text="Taille sprite :")
        row.prop(props, "sprite_size", text="")
        # Info : rendu interne = sprite_size × 4
        sub = box.row()
        sub.enabled = False
        sub.label(text=f"Rendu : {int(props.sprite_size)} px · crop auto · ratio préservé", icon='INFO')

        # ── Pixel Art (encadré) ──────────────────────────────────
        box.separator(factor=0.3)
        pbox = box.box()
        hdr = pbox.row(align=True)
        hdr.prop(props, "pixel_art_enabled", text="")
        hdr.label(text="Pixel Art", icon='TEXTURE')
        if props.pixel_art_enabled:
            col = pbox.column(align=True)
            col.prop(props, "pixel_resolution", text="Résolution")
            row = col.row(align=True)
            row.prop(props, "pixel_show_grid", text="Grille")
            sub = row.row(align=True)
            sub.enabled = props.pixel_show_grid
            sub.prop(props, "pixel_grid_opacity", text="Opacité", slider=True)
            col.prop(props, "pixel_antialias")

            # Contour (coche + épaisseur + couleur)
            pbox.separator(factor=0.2)
            orow = pbox.row(align=True)
            orow.prop(props, "pixel_outline")
            osub = orow.row(align=True)
            osub.enabled = props.pixel_outline
            osub.prop(props, "pixel_outline_thickness", text="Épaisseur")
            osub.prop(props, "pixel_outline_color", text="")

            # Dithering (coche + intensité)
            drow = pbox.row(align=True)
            drow.prop(props, "pixel_dither")
            dsub = drow.row(align=True)
            dsub.enabled = props.pixel_dither
            dsub.prop(props, "pixel_dither_intensity", text="Intensité", slider=True)

            # Actions
            pbox.separator(factor=0.2)
            arow = pbox.row(align=True)
            arow.scale_y = 1.2
            arow.operator("omni.pixel_preview", text="Aperçu pixel art", icon='RENDER_STILL')
            pbox.operator("omni.pixel_refresh_view", text="Recadrer la vue caméra", icon='FILE_REFRESH')

            info = pbox.column(align=True)
            info.enabled = False
            if has_rig:
                info.label(text=f"Sortie : {int(props.sprite_size)} px en gros pixels {int(props.pixel_resolution)}²", icon='INFO')
                info.label(text="L'effet pixel se voit à l'aperçu / l'export (pas dans le viewport)")
            else:
                info.label(text="Créez la caméra iso pour voir la grille", icon='INFO')

        box.separator(factor=0.2)

        # Format + Nb directions
        row = box.row(align=True)
        if is_chars:
            row.prop(props, "export_format", text="")
        row.prop(props, "num_directions", text="Directions")

        # Boutons de sélection des directions (assets uniquement)
        if is_assets:
            box.separator(factor=0.2)
            row = box.row(align=True)
            row.label(text="Angles :")
            row.operator("omni.select_all_dirs",   text="Tout",   icon='RESTRICT_SELECT_OFF')
            row.operator("omni.deselect_all_dirs",  text="Aucun",  icon='RESTRICT_SELECT_ON')

            n = props.num_directions
            cols_per_row = min(n, 8)
            col = box.column(align=True)
            idx = 0
            while idx < n:
                row = col.row(align=True)
                for _ in range(cols_per_row):
                    if idx >= n:
                        break
                    is_sel = bool(props.selected_dirs_mask & (1 << idx))
                    op = row.operator("omni.toggle_dir",
                                      text=_dir_label(idx, n),
                                      depress=is_sel)
                    op.index = idx
                    idx += 1

        # Frames (personnages uniquement)
        if is_chars:
            box.separator(factor=0.2)
            row = box.row(align=True)
            row.prop(props, "frame_start", text="Début")
            row.prop(props, "frame_end",   text="Fin")
            row.operator("omni.sync_frames", text="", icon='FILE_REFRESH')

            row = box.row(align=True)
            row.prop(props, "force_frame_count", text="Limiter")
            if props.force_frame_count:
                total = max(1, props.frame_end - props.frame_start)
                step  = max(1, math.ceil(total / props.forced_frames))
                row.prop(props, "forced_frames", text="Frames cibles")
                row.label(text=f"Step:{step}")
            else:
                row.prop(props, "frame_step", text="Step")

            if props.export_format in ('GIF_DIR', 'GIF_ONE'):
                row = box.row(align=True)
                row.prop(props, "gif_duration", text="ms/frame")
                row.operator("omni.auto_gif_duration", text="", icon='TIME')

        box.separator(factor=0.2)
        row = box.row(align=True)
        row.prop(props, "transparent_bg")
        if is_chars:
            row.prop(props, "export_normals")

        # ── Qualité du rendu (matériaux & lumières) ──────────────
        box.separator(factor=0.3)
        qbox = box.box()
        qbox.label(text="Qualité du rendu", icon='SHADING_RENDERED')

        # Matériaux : couleurs mates / anti-métallique + ombres
        row = qbox.row(align=True)
        row.prop(props, "flat_colors", icon='MATERIAL', toggle=True)
        row.prop(props, "cast_shadows", icon='LIGHT', toggle=True)

        # Lumière principale : on/off + intensité
        qbox.separator(factor=0.2)
        row = qbox.row(align=True)
        row.prop(props, "main_light_on", text="")
        sub = row.row(align=True)
        sub.enabled = props.main_light_on
        sub.prop(props, "main_light_energy", text="Lumière princ.")

        # Spot supérieur (assets uniquement)
        if is_assets:
            row = qbox.row(align=True)
            row.prop(props, "spot_light_on", text="")
            sub = row.row(align=True)
            sub.enabled = props.spot_light_on
            sub.prop(props, "spot_light_energy", text="Spot sup.")

        if not has_rig:
            info = qbox.row()
            info.enabled = False
            info.label(text="Créez la caméra pour piloter les lumières", icon='INFO')

        layout.separator(factor=0.3)

        # ── EXPORT ───────────────────────────────────────────────
        box = layout.box()
        box.label(text="Export", icon='OUTPUT')

        # La coche « sous-dossiers d'origine » n'apparaît QUE si des éléments ont été
        # importés depuis des sous-dossiers. Si elle est cochée, on masque le champ du
        # dossier d'export et on liste à la place les dossiers <sous-dossier>/Render/.
        subdirs     = _export_subdirs(context, is_chars)
        use_subdirs = bool(subdirs) and props.export_to_subdirs
        if subdirs:
            box.prop(props, "export_to_subdirs")

        base = bpy.path.abspath(props.output_path) if props.output_path else os.path.dirname(bpy.data.filepath or "")

        if use_subdirs:
            sub = box.column(align=True)
            sub.enabled = False
            sub.label(text="Rendus exportés vers :", icon='FOLDER_REDIRECT')
            for sd in subdirs:
                sub.label(text=os.path.join(sd, "Render") + os.sep, icon='FILE_FOLDER')
        else:
            row = box.row(align=True)
            op = row.operator("omni.open_folder", text="", icon='FILE_FOLDER')
            op.path = props.output_path
            row.prop(props, "output_path", text="")
            if base:
                render_preview = os.path.join(base, "Render") + os.sep
                sub = box.row()
                sub.enabled = False
                sub.label(text=render_preview, icon='FOLDER_REDIRECT')

        box.separator(factor=0.2)

        # ── Alertes pré-export ───────────────────────────────────
        col_name = COL_CHARACTERS if is_chars else COL_ASSETS
        col_ok = _collection_has_objects(col_name)
        # Fallback sur ancienne vérification imported
        if not col_ok:
            cur_items = context.scene.omni_anim_items if is_chars else context.scene.omni_asset_items
            col_ok = any(getattr(it, "imported", False) for it in cur_items)
        if col_ok:
            col = bpy.data.collections.get(col_name)
            # Compte les ÉLÉMENTS (un par personnage/asset), pas les objets bruts :
            # chaque FBX importé = armature + mesh(es), d'où le double comptage trompeur.
            if col and is_chars:
                cnt = sum(1 for o in col.all_objects if o.type == 'ARMATURE')
                noun_obj = "personnage(s)"
            elif col:
                cnt = len({o.get("sf_asset_name", "") for o in col.all_objects
                           if o.get("sf_asset_name", "")})
                noun_obj = "asset(s)"
            else:
                cnt, noun_obj = 0, "élément(s)"
            info_r = box.row()
            info_r.enabled = False
            info_r.label(text=f"Collection '{col_name}' : {cnt} {noun_obj}", icon='CHECKMARK')
        else:
            noun = "personnages" if is_chars else "assets"
            box.label(text=f"⚠ Validez l'importation des {noun}", icon='ERROR')
        if not has_rig:
            box.label(text="⚠ Créez la caméra iso (section Caméra)", icon='ERROR')

        export_ready = col_ok and has_rig

        if is_chars:
            row = box.row()
            row.enabled = export_ready
            row.operator("omni.export_all", text="Exporter toutes les actions", icon='RENDER_ANIMATION')
            box.separator(factor=0.2)
            box.label(text="Action à exporter :")
            row = box.row(align=True)
            row.enabled = export_ready
            row.prop(props, "selected_action", text="")
            row.operator("omni.export_specific", text="", icon='RENDER_STILL')
        else:
            row = box.row()
            row.enabled = export_ready
            row.operator("omni.export_assets", text="Exporter tous les assets", icon='RENDER_ANIMATION')
            box.separator(factor=0.2)
            box.label(text="Asset à exporter :")
            row = box.row(align=True)
            row.enabled = export_ready
            row.prop(props, "selected_asset_idx", text="")
            row.operator("omni.export_single_asset", text="", icon='RENDER_STILL')


class OMNI_OT_open_folder(Operator):
    bl_idname      = "omni.open_folder"
    bl_label       = "Ouvrir le dossier"
    bl_description = "Ouvre ce dossier dans l'Explorateur Windows"
    path: StringProperty(default="")

    def execute(self, context):
        path = bpy.path.abspath(self.path).strip() if self.path else ""
        if not path or not os.path.isdir(path):
            self.report({'WARNING'}, "Dossier invalide ou inexistant.")
            return {'CANCELLED'}
        import subprocess
        subprocess.Popen(['explorer', os.path.normpath(path)])
        return {'FINISHED'}


class OMNI_OT_reset_cam_offset(Operator):
    bl_idname      = "omni.reset_cam_offset"
    bl_label       = "Réinitialiser position"
    bl_description = "Remet les offsets X/Y/Z du pivot caméra à zéro"

    def execute(self, context):
        props = context.scene.omni_render_settings
        props.cam_offset_x = 0.0
        props.cam_offset_y = 0.0
        props.cam_offset_z = 0.0
        rig = _get_cam_rig()
        if rig:
            rig.location = (0.0, 0.0, 0.0)
        return {'FINISHED'}


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTER
# ══════════════════════════════════════════════════════════════════════════════

classes = [
    OmniAnimItem,
    OmniAssetItem,
    OmniRenderSettings,
    OMNI_PT_panel,
    OMNI_OT_scan_fbx,
    OMNI_OT_remove_anim_item,
    OMNI_OT_import_selected,
    OMNI_OT_scan_assets,
    OMNI_OT_remove_asset_item,
    OMNI_OT_import_assets,
    OMNI_OT_clear_anim_list,
    OMNI_OT_clear_asset_list,
    OMNI_OT_toggle_dir,
    OMNI_OT_select_all_dirs,
    OMNI_OT_deselect_all_dirs,
    OMNI_OT_export_single_asset,
    OMNI_OT_export_assets,
    OMNI_OT_setup_camera,
    OMNI_OT_add_asset_light,
    OMNI_OT_cam_zoom,
    OMNI_OT_cam_pan,
    OMNI_OT_cam_to_view,
    OMNI_OT_pixel_refresh_view,
    OMNI_OT_pixel_preview,
    OMNI_OT_open_folder,
    OMNI_OT_reset_cam_offset,
    OMNI_OT_auto_gif_duration,
    OMNI_OT_sync_frames,
    OMNI_OT_export_all,
    OMNI_OT_export_specific,
]


def _delayed_apply_quality(scene):
    """Appelé via timer après le chargement pour s'assurer que tous les matériaux existent."""
    try:
        _apply_quality_settings(bpy.context.scene)
    except Exception:
        pass
    return None  # retourne None = ne se répète pas


@bpy.app.handlers.persistent
def _on_load_post(dummy):
    """Au chargement d'un .blend : applique les réglages qualité (mat, ombres, lumières)."""
    try:
        _apply_quality_settings(bpy.context.scene)
    except Exception:
        pass
    # Second passage après 0.1 s pour couvrir les matériaux chargés tardivement
    try:
        bpy.app.timers.register(_delayed_apply_quality, first_interval=0.1)
    except Exception:
        pass


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.omni_render_settings = PointerProperty(type=OmniRenderSettings)
    bpy.types.Scene.omni_anim_items      = CollectionProperty(type=OmniAnimItem)
    bpy.types.Scene.omni_asset_items     = CollectionProperty(type=OmniAssetItem)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    # Applique immédiatement pour la session courante (addon activé sur fichier déjà ouvert)
    try:
        _apply_quality_settings(bpy.context.scene)
    except Exception:
        pass


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.omni_render_settings
    del bpy.types.Scene.omni_anim_items
    del bpy.types.Scene.omni_asset_items


if __name__ == "__main__":
    register()
