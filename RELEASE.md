# 🔥 SpriteForge v2.1 – Release

**Export de sprites isométriques multi-directions automatisé pour Blender**

---

## 📌 Vue d'ensemble

SpriteForge v2.1 est une mise à jour majeure qui consolidate toutes les fonctionnalités de création de sprites 2D isométriques. Cet addon automatise complètement le pipeline d'export de personnages FBX animés et d'assets 3D statiques vers des sprites prêts pour la production.

**Compatibilité :** Blender 5.1.1+ | Python 3.11+

---

## ✨ Nouvelles fonctionnalités

### 🎨 Mode Pixel Art amélioré
- **Grille de visualisation** en temps réel dans la vue 3D
- **Résolutions configurables** : 16px à 256px
- **Outline automatique** avec couleur personnalisable
- **Dithering Floyd-Steinberg** réglable (0-100%)
- **Anti-aliasing optionnel** pour lissage des bords
- **Réduction de palette** intelligente

### 🧍 Détection automatique d'animations
- Scan automatique des dossiers FBX
- Détection des Actions Blender
- Synchronisation des frames
- Gestion du root motion
- Export individuel ou batch

### 🏠 Mode Assets optimisé
- Import multi-formats : FBX, OBJ, GLB, GLTF, DAE
- Rotation multi-directions configurable
- Caméra orthographique dédiée
- Éclairage spot supérieur pour assets
- Auto-crop intelligent

### 📷 Caméra isométrique avancée
- **Création automatisée** du rig caméra
- **Zoom et pan rapides** avec contrôles UI
- **Décalage XYZ flexible** pour ajustements fins
- **Prévisualisation pixel art** en temps réel
- **Recadrage automatique** intelligent

### 📦 Formats d'export
- **PNG** : une image par frame
- **Spritesheet** : grille complète optimisée
- **GIF par direction** : animations par angle
- **GIF unique** : 360° dans un seul GIF
- **Normal Maps** : export de cartes normales pour moteurs 3D

### 💡 Éclairage professionnel
- **Couleurs mates** (suppression des reflets métalliques)
- **Gestion des ombres** configurable
- **Lumière principale** avec intensité réglable
- **Spot supérieur** pour l'éclairage des assets
- **Matériaux optimisés** pour jeux 2D

---

## 🔧 Améliorations techniques

### Moteur de rendu
```python
- Supersampling interne 4x (INTERNAL_RENDER_MULT = 4)
- Redimensionnement LANCZOS pour qualité maximale
- Marge de sécurité caméra 1.25x (RENDER_SAFETY_ZOOM)
- Auto-crop avec détection de boîte englobante globale
```

### Collections intelligentes
- Hiérarchie SpriteForge automatisée
  - SpriteForge (racine)
  - SpriteForge Characters
  - SpriteForge Assets
  - SpriteForge Camera

### Pipeline d'animation
- Détection automatique des armatures
- Synchronisation des frames par action
- Gestion du root motion
- Export multi-personnages

---

## 🎮 Workflows

### Personnages animés
1. Importer dossier FBX (avec armature)
2. Scanner → détection auto des animations
3. Vérifier cadrage en vue caméra
4. Sélectionner format d'export
5. Cliquer « Exporter »

### Assets statiques
1. Importer dossier (FBX, OBJ, etc.)
2. Définir nombre de directions
3. Ajuster cadrage
4. Sélectionner format
5. Cliquer « Exporter »

---

## 📊 Caractéristiques principales

| Fonctionnalité | Détails |
|---|---|
| **Import** | FBX, OBJ, GLB, GLTF, DAE (+ textures) |
| **Export** | PNG, Spritesheet, GIF, Normal Maps |
| **Directions** | 1-8+ directions configurables |
| **Résolutions** | 32x32 à 1024x1024 pixels |
| **Pixel Art** | Grille, outline, dithering, AA |
| **Éclairage** | Main light + spot supérieur |
| **Caméra** | Orthographique isométrique |
| **Optimisations** | Auto-crop, couleurs mates, root motion |

---

## ⚙️ Configuration requise

### Système
- **Windows**, macOS, ou Linux
- **Blender 5.1.1+** (testé et validé)
- **Python 3.11+**

### Dépendances Python
- **PIL/Pillow** (installation automatique)
- **NumPy** (inclus avec Blender)
- **OpenGL** (natif)

---

## 🚀 Installation

1. **Télécharger** le fichier ZIP
2. **Édition → Préférences → Add-ons**
3. **Installer depuis un fichier**
4. Sélectionner le ZIP et **activer**
5. **N** dans la vue 3D → onglet **SpriteForge**

---

## 🐛 Corrections depuis v2.0

- ✅ Performance d'export améliorée (rendering x4)
- ✅ Détection d'armature robustifiée
- ✅ Gestion des offsets caméra plus stable
- ✅ Dithering Floyd-Steinberg optimisé
- ✅ Gestion des sous-dossiers améliorée
- ✅ UI plus intuitive et responsive

---

## 📖 Documentation

- **README.md** – Guide complet d'utilisation
- **index.html** – Documentation interactive
- **Widgets Blender** – Aide intégrée sur chaque paramètre

---

## 💡 Conseils de pro

🎯 Testez le cadrage avant d'exporter  
🎯 Organisez vos dossiers par ressource  
🎯 Utilisez l'éclairage intégré (plus efficace)  
🎯 Exportez en sous-dossiers (par direction)  
🎯 Testez différentes résolutions  

---

## 🔄 Roadmap

- [ ] Support animation par spritesheet
- [ ] Export WebP et AVIF
- [ ] Batch processing multi-personnages
- [ ] Intégration Godot directe
- [ ] GUI avancée pour pixel art

---

## ❤️ Crédits

**Cortex** – Game Dev • Artiste 2D • Animateur • Pixel Artist

Merci d'utiliser SpriteForge ! 🔥

---

## 📝 Notes de version

**v2.1** – Release majeure  
- Stabilisation complète de tous les modules
- Documentation revue et améliorée
- Optimisations de performance
- Support complet Blender 5.1.1

---

## 📞 Support

- 🐛 [Signaler un bug](https://github.com/Oderc95/SpriteForge/issues)
- 💻 [Code source](https://github.com/Oderc95/SpriteForge)
- 📚 [Documentation](https://github.com/Oderc95/SpriteForge/blob/main/README.md)

---

<div align="center">

**[⬇️ Télécharger SpriteForge v2.1](https://github.com/Oderc95/SpriteForge/releases)**

</div>
