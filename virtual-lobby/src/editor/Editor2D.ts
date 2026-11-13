/**
 * 2D Top-Down Editor
 *
 * Canvas-based editor for placing and manipulating world elements.
 * Supports pan, zoom, and various tools (select, wall, fruit, note).
 */

import { WorldState, VirtualElement, VirtualPlayer } from '../WorldState';

const GRID_SIZE = 1; // 1 meter grid
const COLORS = {
  background: '#0f172a',
  grid: '#1e293b',
  gridMajor: '#334155',
  wall: '#64748b',
  fruit: '#f59e0b',
  note: '#3b82f6',
  player: '#22c55e',
  selection: '#60a5fa',
};

// Callback type for syncing element changes with API
export type ElementChangeCallback = (
  action: 'add' | 'update' | 'remove',
  element: VirtualElement | Partial<VirtualElement>,
  id?: number
) => void;

export class Editor2D {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private worldState: WorldState;
  private onElementChange: ElementChangeCallback | null = null;

  // View state
  private panX = 0;
  private panY = 0;
  private zoom = 1;

  // Interaction state
  private tool = 'select';
  private isDragging = false;
  private dragStart = { x: 0, y: 0 };
  private selectedElement: VirtualElement | null = null;
  private drawingElement: Partial<VirtualElement> | null = null;

  constructor(canvas: HTMLCanvasElement, worldState: WorldState) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d')!;
    this.worldState = worldState;

    this.setupEventListeners();
  }

  // Set callback for syncing changes with server (optional)
  setOnElementChange(callback: ElementChangeCallback | null) {
    this.onElementChange = callback;
  }

  private setupEventListeners() {
    // Mouse events
    this.canvas.addEventListener('mousedown', this.onMouseDown.bind(this));
    this.canvas.addEventListener('mousemove', this.onMouseMove.bind(this));
    this.canvas.addEventListener('mouseup', this.onMouseUp.bind(this));
    this.canvas.addEventListener('wheel', this.onWheel.bind(this));

    // Context menu (prevent default for right-click pan)
    this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    // Keyboard events for delete
    window.addEventListener('keydown', this.onKeyDown.bind(this));
  }

  private onKeyDown(e: KeyboardEvent) {
    if ((e.key === 'Delete' || e.key === 'Backspace') && this.selectedElement) {
      const id = this.selectedElement.id;
      this.worldState.removeElement(id);
      if (this.onElementChange) {
        this.onElementChange('remove', this.selectedElement, id);
      }
      this.selectedElement = null;
      this.render();
    }
  }

  setTool(tool: string) {
    this.tool = tool;
    this.selectedElement = null;
    this.canvas.style.cursor = tool === 'select' ? 'default' : 'crosshair';
  }

  setZoom(zoom: number) {
    this.zoom = Math.max(0.1, Math.min(5, zoom));
    this.render();
  }

  resetView() {
    this.panX = 0;
    this.panY = 0;
    this.zoom = 1;
    this.render();
  }

  resize() {
    const rect = this.canvas.parentElement?.getBoundingClientRect();
    if (rect) {
      this.canvas.width = rect.width;
      this.canvas.height = rect.height;
      this.render();
    }
  }

  // Convert screen coordinates to world coordinates
  private screenToWorld(screenX: number, screenY: number): { x: number; y: number } {
    const centerX = this.canvas.width / 2;
    const centerY = this.canvas.height / 2;
    const scale = 50 * this.zoom; // pixels per meter

    return {
      x: (screenX - centerX - this.panX) / scale,
      y: -(screenY - centerY - this.panY) / scale, // Y is inverted
    };
  }

  // Convert world coordinates to screen coordinates
  private worldToScreen(worldX: number, worldY: number): { x: number; y: number } {
    const centerX = this.canvas.width / 2;
    const centerY = this.canvas.height / 2;
    const scale = 50 * this.zoom;

    return {
      x: centerX + this.panX + worldX * scale,
      y: centerY + this.panY - worldY * scale,
    };
  }

  private onMouseDown(e: MouseEvent) {
    const worldPos = this.screenToWorld(e.offsetX, e.offsetY);

    // Right-click: start panning
    if (e.button === 2) {
      this.isDragging = true;
      this.dragStart = { x: e.offsetX - this.panX, y: e.offsetY - this.panY };
      return;
    }

    // Left-click: tool action
    if (this.tool === 'select') {
      this.selectedElement = this.worldState.getElementAt(worldPos.x, worldPos.y) || null;
      if (this.selectedElement) {
        this.isDragging = true;
        this.dragStart = { x: worldPos.x, y: worldPos.y };
      }
    } else if (this.tool === 'wall') {
      this.drawingElement = {
        element_type: 'wall',
        x: worldPos.x,
        y: worldPos.y,
        z: 0,
        width: 0,
        height: 0,
        depth: 2,
        rotation: 0,
      };
      this.isDragging = true;
      this.dragStart = { x: worldPos.x, y: worldPos.y };
    } else if (this.tool === 'fruit' || this.tool === 'note') {
      // Place immediately
      const element: VirtualElement = {
        id: this.worldState.generateLocalId(),
        element_type: this.tool,
        x: worldPos.x,
        y: worldPos.y,
        z: 0.5,
        width: 0.5,
        height: 0.5,
        rotation: 0,
      };
      // Add to local state
      this.worldState.addElement(element);
      // Notify API if connected
      if (this.onElementChange) {
        this.onElementChange('add', element);
      }
    }

    this.render();
  }

  private onMouseMove(e: MouseEvent) {
    if (!this.isDragging) return;

    // Panning
    if (e.buttons === 2) {
      this.panX = e.offsetX - this.dragStart.x;
      this.panY = e.offsetY - this.dragStart.y;
      this.render();
      return;
    }

    const worldPos = this.screenToWorld(e.offsetX, e.offsetY);

    // Dragging selected element
    if (this.tool === 'select' && this.selectedElement) {
      this.selectedElement.x = worldPos.x;
      this.selectedElement.y = worldPos.y;
      this.worldState.updateElement(this.selectedElement.id, {
        x: worldPos.x,
        y: worldPos.y,
      });
      // Note: We'll notify API on mouse up to avoid spamming
      return;
    }

    // Drawing wall
    if (this.tool === 'wall' && this.drawingElement) {
      const dx = worldPos.x - this.dragStart.x;
      const dy = worldPos.y - this.dragStart.y;
      this.drawingElement.width = Math.abs(dx);
      this.drawingElement.height = Math.abs(dy);
      this.drawingElement.x = this.dragStart.x + dx / 2;
      this.drawingElement.y = this.dragStart.y + dy / 2;
      this.render();
    }
  }

  private onMouseUp(_e: MouseEvent) {
    // Notify API of element position update after drag
    if (this.tool === 'select' && this.selectedElement && this.isDragging) {
      if (this.onElementChange) {
        this.onElementChange('update', {
          x: this.selectedElement.x,
          y: this.selectedElement.y,
        }, this.selectedElement.id);
      }
    }

    if (this.tool === 'wall' && this.drawingElement && this.drawingElement.width! > 0.1) {
      // Create the wall element with a local ID
      const element: VirtualElement = {
        id: this.worldState.generateLocalId(),
        element_type: 'wall',
        x: this.drawingElement.x!,
        y: this.drawingElement.y!,
        z: this.drawingElement.z || 0,
        width: this.drawingElement.width,
        height: this.drawingElement.height,
        depth: this.drawingElement.depth || 2,
        rotation: this.drawingElement.rotation || 0,
      };
      // Add to local state
      this.worldState.addElement(element);
      // Notify API if connected
      if (this.onElementChange) {
        this.onElementChange('add', element);
      }
    }

    this.isDragging = false;
    this.drawingElement = null;
    this.render();
  }

  private onWheel(e: WheelEvent) {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    this.zoom = Math.max(0.1, Math.min(5, this.zoom * delta));
    this.render();
  }

  render() {
    const { ctx, canvas } = this;
    const width = canvas.width;
    const height = canvas.height;

    // Clear
    ctx.fillStyle = COLORS.background;
    ctx.fillRect(0, 0, width, height);

    // Draw grid
    this.drawGrid();

    // Draw elements
    for (const element of this.worldState.elements) {
      this.drawElement(element, element === this.selectedElement);
    }

    // Draw drawing preview
    if (this.drawingElement) {
      this.drawElement(this.drawingElement as VirtualElement, false, 0.5);
    }

    // Draw players
    for (const player of this.worldState.players) {
      this.drawPlayer(player);
    }

    // Draw origin marker
    const origin = this.worldToScreen(0, 0);
    ctx.strokeStyle = '#ef4444';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(origin.x - 10, origin.y);
    ctx.lineTo(origin.x + 10, origin.y);
    ctx.moveTo(origin.x, origin.y - 10);
    ctx.lineTo(origin.x, origin.y + 10);
    ctx.stroke();
  }

  private drawGrid() {
    const { ctx, canvas } = this;
    const scale = 50 * this.zoom;
    const gridSpacing = GRID_SIZE * scale;

    const offsetX = (canvas.width / 2 + this.panX) % gridSpacing;
    const offsetY = (canvas.height / 2 + this.panY) % gridSpacing;

    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;

    // Vertical lines
    for (let x = offsetX; x < canvas.width; x += gridSpacing) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, canvas.height);
      ctx.stroke();
    }

    // Horizontal lines
    for (let y = offsetY; y < canvas.height; y += gridSpacing) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(canvas.width, y);
      ctx.stroke();
    }
  }

  private drawElement(element: VirtualElement, selected: boolean, alpha = 1) {
    const { ctx } = this;
    const pos = this.worldToScreen(element.x, element.y);
    const scale = 50 * this.zoom;

    const w = (element.width || 1) * scale;
    const h = (element.height || 1) * scale;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(pos.x, pos.y);
    ctx.rotate(-element.rotation);

    // Fill
    switch (element.element_type) {
      case 'wall':
        ctx.fillStyle = COLORS.wall;
        break;
      case 'fruit':
        ctx.fillStyle = COLORS.fruit;
        break;
      case 'note':
        ctx.fillStyle = COLORS.note;
        break;
      default:
        ctx.fillStyle = '#888888';
    }

    if (element.element_type === 'wall') {
      ctx.fillRect(-w / 2, -h / 2, w, h);
    } else {
      // Draw as circle for fruit/note
      ctx.beginPath();
      ctx.arc(0, 0, scale * 0.3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Selection highlight
    if (selected) {
      ctx.strokeStyle = COLORS.selection;
      ctx.lineWidth = 3;
      if (element.element_type === 'wall') {
        ctx.strokeRect(-w / 2 - 2, -h / 2 - 2, w + 4, h + 4);
      } else {
        ctx.beginPath();
        ctx.arc(0, 0, scale * 0.35, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    ctx.restore();
  }

  private drawPlayer(player: VirtualPlayer) {
    const { ctx } = this;
    const pos = this.worldToScreen(player.x, player.y);
    const scale = 50 * this.zoom;
    const size = scale * 0.5;

    ctx.save();
    ctx.translate(pos.x, pos.y);
    ctx.rotate(-player.yaw);

    // Player body (square)
    ctx.fillStyle = player.color;
    ctx.fillRect(-size / 2, -size / 2, size, size);

    // Direction indicator (triangle)
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.moveTo(size / 2, 0);
    ctx.lineTo(0, -size / 4);
    ctx.lineTo(0, size / 4);
    ctx.closePath();
    ctx.fill();

    // Name label
    ctx.fillStyle = '#ffffff';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(player.name, 0, -size / 2 - 8);

    ctx.restore();
  }
}
