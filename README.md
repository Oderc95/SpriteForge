# 🔥 SpriteForge

<p align="center">
  <img src="assets/logo.png" width="600" alt="SpriteForge Logo">
</p>

<p align="center">
  <strong>Transformez vos modèles 3D en sprites 2D isométriques en quelques clics</strong>
</p>

<p align="center">
  Créé par <strong>Cortex</strong> | Game dev • Artiste 2D • Animateur • Pixel Artist
</p>

---

## ⚡ Fonctionnalités principales

✨ **Import automatique** – FBX, OBJ, GLB, GLTF, DAE  
🎮 **Export multi-directions** – 8 directions par défaut, configurable  
🖼️ **Formats multiples** – PNG, Spritesheet, GIF par direction, GIF unique  
🎨 **Pixel Art natif** – Grille, outline, dithering, anti-aliasing  
📷 **Caméra isométrique** – Orthographique optimisée, zoom, pan, offset  
✂️ **Auto-crop intelligent** – Détection automatique du cadrage  
💡 **Éclairage professionnel** – Couleurs mates, ombres, gestion des lumières  
🗺️ **Normal Maps** – Export de cartes normales  
🧍 **Personnages animés** – Détection automatique des animations  
🏠 **Assets statiques** – Rotation multi-directions optimisée  

---

## 📋 Compatibilité

| Élément | Exigence |
|---------|----------|
| **Blender** | 5.1.1+ |
| **Python** | 3.11+ |
| **OS** | Windows, macOS, Linux |

> ⚠️ L'utilisation sur d'autres versions de Blender n'est pas officiellement garantie.

---

## 🚀 Installation

### 1️⃣ Télécharger l'addon

Récupérez la dernière version depuis les [Releases](https://github.com/Oderc95/SpriteForge/releases).

### 2️⃣ Installer dans Blender

```
Édition → Préférences → Add-ons → Installer depuis un fichier
```

Sélectionnez le fichier **ZIP** de SpriteForge.

### 3️⃣ Activer l'addon

Cherchez **Cortex SpriteForge** et cochez la case d'activation.

### 4️⃣ Accéder au panneau

- Ouvrez Blender
- Allez dans l'onglet **Layout**
- Appuyez sur **N** pour ouvrir la barre latérale
- Cliquez sur **SpriteForge**

---

## 🎮 Démarrage rapide

### Mode Personnages (FBX animés)

```
1. Choisir "Characters"
2. Sélectionner le dossier contenant les FBX
3. Cliquer sur "Scanner" (détection automatique)
4. Vérifier le cadrage dans la caméra
5. Sélectionner le format d'export
6. Cliquer sur "Exporter"
```

### Mode Assets (Objets statiques)

```
1. Choisir "Assets"
2. Sélectionner le dossier des modèles
3. Cliquer sur "Scanner"
4. Définir le nombre de directions
5. Ajuster le cadrage si nécessaire
6. Cliquer sur "Exporter"
```

---

## 🎨 Modes d'export

| Format | Description |
|--------|-------------|
| **PNG** | Une image par frame |
| **Spritesheet** | Toutes les frames dans une grille |
| **GIF Direction** | Un GIF animé par angle |
| **GIF Unique** | Toutes les directions dans 1 GIF |
| **Normal Maps** | Cartes normales compatibles moteurs |

---

## 🟪 Mode Pixel Art

Transformez vos rendus en pixel art authentique avec ces outils :

- **Grille de visualisation** – Affiche les pixels en temps réel
- **Résolutions** – De 16px à 256px
- **Outline** – Contour automatique (noir ou couleur personnalisée)
- **Dithering** – Effet rétro Floyd-Steinberg
- **Anti-aliasing** – Lissage optionnel des bords
- **Réduction de palette** – Compression des couleurs

**Configuration recommandée pour pixel art rétro :**
```
Pixel Art:        ON
Outline:          ON (1-2px)
Anti-aliasing:    OFF
Dithering:        ON (30-50%)
```

---

## 📷 Caméra isométrique

La caméra SpriteForge est préconfigurée pour les rendus isométriques :

- **Orthographique** – Pas de perspective
- **Contrôles rapides** – Zoom, pan, décalage XYZ
- **Auto-crop** – Détection automatique des marges
- **Prévisualisation** – Vérification temps réel

**Pour ajuster le cadrage :**
1. Sélectionner le pivot caméra (CortexCamRig)
2. Utiliser les contrôles de zoom et pan
3. Modifier les offsets X/Y/Z si nécessaire
4. Vérifier le rendu dans la vue caméra

---

## ⚙️ Configuration avancée

### Couleurs mates (Assets Meshy)

Désactive les reflets métalliques excessifs :
```
Couleurs mates: ON
```

### Gestion des ombres

Contrôlez l'aspect des ombres :
```
Ombres: ON/OFF
Lumière principale: Ajuster l'intensité
Spot supérieur: Pour les assets
```

### Optimisations pour jeux HD-2D

```
Pixel Art:       OFF
Couleurs mates:  ON
Ombres:          ON
Anti-aliasing:   ON
```

---

## ⚠️ Dépannage

### Le panneau SpriteForge n'apparaît pas

✓ Vérifier que l'addon est **activé**  
✓ Être dans l'espace de travail **Layout**  
✓ Appuyer sur **N** pour afficher la sidebar  
✓ Sélectionner l'onglet **SpriteForge**  

### Les sprites sont coupés

→ Augmenter la **largeur/hauteur de la caméra**  
→ Ajuster les **offsets XYZ**  

### Les animations ne sont pas détectées

✓ Vérifier la présence d'une **armature**  
✓ Vérifier les **Actions Blender**  
✓ Vérifier l'export FBX d'origine  

### Le rendu est flou en Pixel Art

→ Désactiver l'**anti-aliasing**  
→ Réduire la **résolution pixel**  

### Les modèles sont mal importés

✓ Utiliser les **formats supportés** (FBX, OBJ, GLB, GLTF, DAE)  
✓ Vérifier les **chemins de fichier** (pas d'espaces)  
✓ S'assurer que les **matériaux/textures** sont embarqués  

---

## 📊 Formats supportés

**Personnages :**
- `.fbx` (recommended for animations)

**Assets :**
- `.fbx` – Modèles 3D
- `.obj` – Wavefront OBJ
- `.glb` – Binary glTF
- `.gltf` – glTF avec textures
- `.dae` – COLLADA

---

## 💡 Conseils pro

🎯 **Testez le cadrage d'abord** – Vérifiez dans la vue caméra avant l'export  
🎯 **Déléguez les lumières** – Utilisez l'éclairage intégré plutôt que des matériaux complexes  
🎯 **Organisez vos dossiers** – Un dossier = une ressource (FBX + textures)  
🎯 **Exportez en sous-dossiers** – Gardez vos rendus organisés par direction  
🎯 **Testez les différentes résolutions** – 64px, 128px ou 256px selon votre jeu  

---

## 🐛 Signaler un bug

Trouvez un problème ? Créez une [Issue](https://github.com/Oderc95/SpriteForge/issues) en décrivant :

- La version de Blender utilisée
- Les étapes pour reproduire
- Le message d'erreur (si applicable)
- Une capture d'écran ou vidéo

---

## 📜 Licence

Ce projet est protégé par sa propre licence. Voir le fichier [LICENSE](LICENSE).

---

## ❤️ Crédit

**Cortex** – Game dev • Artiste 2D • Animateur • Pixel Artist

Merci d'utiliser SpriteForge ! 🔥
