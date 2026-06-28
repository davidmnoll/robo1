/**
 * 3D Scene Renderer
 *
 * Three.js-based 3D renderer for the virtual world.
 * Renders elements and players, supports first-person views per player.
 */

import * as THREE from 'three';
import { WorldState, VirtualElement, VirtualPlayer } from '../WorldState';

export class Scene3D {
  private canvas: HTMLCanvasElement;
  private worldState: WorldState;

  private scene: THREE.Scene;
  private camera: THREE.PerspectiveCamera;
  private renderer: THREE.WebGLRenderer;

  // Object maps for efficient updates
  private elementMeshes: Map<number, THREE.Mesh> = new Map();
  private playerMeshes: Map<string, THREE.Group> = new Map();

  // Active player for first-person view (null = overview)
  private activePlayer: string | null = null;

  constructor(canvas: HTMLCanvasElement, worldState: WorldState) {
    this.canvas = canvas;
    this.worldState = worldState;

    // Initialize Three.js
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1a1a2e);

    // Camera
    this.camera = new THREE.PerspectiveCamera(75, canvas.width / canvas.height, 0.1, 1000);
    this.camera.position.set(0, 10, 10);
    this.camera.lookAt(0, 0, 0);

    // Renderer
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setSize(canvas.width, canvas.height);
    this.renderer.setPixelRatio(window.devicePixelRatio);

    // Lighting
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.5);
    this.scene.add(ambientLight);

    const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
    directionalLight.position.set(5, 10, 5);
    this.scene.add(directionalLight);

    // Ground plane
    const groundGeometry = new THREE.PlaneGeometry(100, 100);
    const groundMaterial = new THREE.MeshStandardMaterial({
      color: 0x334155,
      roughness: 0.8,
    });
    const ground = new THREE.Mesh(groundGeometry, groundMaterial);
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = 0;
    this.scene.add(ground);

    // Grid helper
    const gridHelper = new THREE.GridHelper(100, 100, 0x475569, 0x1e293b);
    gridHelper.position.y = 0.01;
    this.scene.add(gridHelper);
  }

  resize() {
    // Note: Using fixed size for preview window rather than parent size
    // Use fixed size for preview window
    const width = 320;
    const height = 240;

    this.canvas.width = width;
    this.canvas.height = height;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  }

  setActivePlayer(namespace: string | null) {
    this.activePlayer = namespace;
  }

  update() {
    // Update camera for first-person view
    if (this.activePlayer) {
      const player = this.worldState.getPlayer(this.activePlayer);
      if (player) {
        // First-person camera position inside the player cube
        this.camera.position.set(player.x, player.z + 0.4, player.y);
        // Look forward based on yaw
        const lookDir = new THREE.Vector3(
          Math.sin(player.yaw),
          0,
          -Math.cos(player.yaw)
        );
        this.camera.lookAt(
          player.x + lookDir.x,
          player.z + 0.4,
          player.y + lookDir.z
        );
      }
    } else {
      // Overview camera
      this.camera.position.set(0, 15, 15);
      this.camera.lookAt(0, 0, 0);
    }
  }

  render() {
    this.syncElements();
    this.syncPlayers();
    this.renderer.render(this.scene, this.camera);
  }

  private syncElements() {
    const currentIds = new Set(this.worldState.elements.map(e => e.id));

    // Remove deleted elements
    for (const [id, mesh] of this.elementMeshes) {
      if (!currentIds.has(id)) {
        this.scene.remove(mesh);
        this.elementMeshes.delete(id);
      }
    }

    // Add/update elements
    for (const element of this.worldState.elements) {
      let mesh = this.elementMeshes.get(element.id);

      if (!mesh) {
        mesh = this.createElementMesh(element);
        this.scene.add(mesh);
        this.elementMeshes.set(element.id, mesh);
      }

      // Update position/rotation
      mesh.position.set(element.x, element.z || 0, element.y);
      mesh.rotation.y = element.rotation;

      // Update scale for walls
      if (element.element_type === 'wall') {
        mesh.scale.set(
          element.width || 1,
          element.depth || 2,
          element.height || 1
        );
        mesh.position.y = (element.depth || 2) / 2;
      }
    }
  }

  private createElementMesh(element: VirtualElement): THREE.Mesh {
    let geometry: THREE.BufferGeometry;
    let material: THREE.Material;

    switch (element.element_type) {
      case 'wall':
        geometry = new THREE.BoxGeometry(1, 1, 1);
        material = new THREE.MeshStandardMaterial({ color: 0x64748b });
        break;

      case 'fruit':
        geometry = new THREE.SphereGeometry(0.3, 16, 16);
        material = new THREE.MeshStandardMaterial({ color: 0xf59e0b });
        break;

      case 'note':
        geometry = new THREE.BoxGeometry(0.5, 0.5, 0.1);
        material = new THREE.MeshStandardMaterial({ color: 0x3b82f6 });
        break;

      default:
        geometry = new THREE.BoxGeometry(0.5, 0.5, 0.5);
        material = new THREE.MeshStandardMaterial({ color: 0x888888 });
    }

    return new THREE.Mesh(geometry, material);
  }

  private syncPlayers() {
    const currentNamespaces = new Set(this.worldState.players.map(p => p.namespace));

    // Remove deleted players
    for (const [namespace, group] of this.playerMeshes) {
      if (!currentNamespaces.has(namespace)) {
        this.scene.remove(group);
        this.playerMeshes.delete(namespace);
      }
    }

    // Add/update players
    for (const player of this.worldState.players) {
      // Skip active player in first-person mode (we're inside them)
      if (this.activePlayer === player.namespace) continue;

      let group = this.playerMeshes.get(player.namespace);

      if (!group) {
        group = this.createPlayerMesh(player);
        this.scene.add(group);
        this.playerMeshes.set(player.namespace, group);
      }

      // Update position/rotation
      group.position.set(player.x, player.z, player.y);
      group.rotation.y = player.yaw;

      // Update color if changed
      const bodyMesh = group.children[0] as THREE.Mesh;
      const material = bodyMesh.material as THREE.MeshStandardMaterial;
      const newColor = new THREE.Color(player.color);
      if (!material.color.equals(newColor)) {
        material.color = newColor;
      }
    }
  }

  private createPlayerMesh(player: VirtualPlayer): THREE.Group {
    const group = new THREE.Group();

    // Body (cube)
    const bodyGeometry = new THREE.BoxGeometry(0.8, 0.8, 0.8);
    const bodyMaterial = new THREE.MeshStandardMaterial({ color: player.color });
    const body = new THREE.Mesh(bodyGeometry, bodyMaterial);
    body.position.y = 0.4;
    group.add(body);

    // Direction indicator (cone)
    const indicatorGeometry = new THREE.ConeGeometry(0.15, 0.3, 8);
    const indicatorMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff });
    const indicator = new THREE.Mesh(indicatorGeometry, indicatorMaterial);
    indicator.rotation.z = -Math.PI / 2;
    indicator.position.set(0.55, 0.4, 0);
    group.add(indicator);

    return group;
  }

  // Get the canvas stream for WebRTC
  captureStream(fps = 30): MediaStream {
    return this.canvas.captureStream(fps);
  }
}
