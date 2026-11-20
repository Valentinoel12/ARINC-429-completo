"""GUI didáctica Tkinter para administrar instrumentos y envíos ARINC 429.

La escala y codificación del campo de datos son demostrativas; no conectar a
aviónica. La salida serial está pensada exclusivamente para una interfaz aislada
de laboratorio y no sustituye un transmisor ARINC 429 certificado.
"""

from __future__ import annotations

import json
import math
import uuid
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import messagebox, ttk


DATA_BITS = 19
MAX_DATA = (1 << DATA_BITS) - 1


@dataclass
class Instrument:
	id: str
	name: str
	minimum: float
	maximum: float
	label: int
	sdi: int = 0


@dataclass
class Shipment:
	id: str
	instrument_id: str
	value: float


class JsonStore:
	"""Almacenamiento JSON sencillo para los dos CRUD."""

	def __init__(self, path: Path) -> None:
		self.path = path
		self.instruments: dict[str, Instrument] = {}
		self.shipments: dict[str, Shipment] = {}
		self.load()

	def load(self) -> None:
		if not self.path.exists():
			return
		try:
			data = json.loads(self.path.read_text(encoding="utf-8"))
			self.instruments = {
				item["id"]: Instrument(**item)
				for item in data.get("instruments", [])
			}
			self.shipments = {
				item["id"]: Shipment(**item)
				for item in data.get("shipments", [])
			}
		except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
			raise ValueError(f"El archivo de datos no es válido: {exc}") from exc

	def save(self) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		payload = {
			"instruments": [asdict(item) for item in self.instruments.values()],
			"shipments": [asdict(item) for item in self.shipments.values()],
		}
		temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
		temporary_path.write_text(
			json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
		)
		temporary_path.replace(self.path)


def new_id() -> str:
	return uuid.uuid4().hex[:8]


def reverse_label_bits(label: int) -> int:
	"""Invierte el orden de los ocho bits de etiqueta para la palabra."""
	return int(f"{label:08b}"[::-1], 2)


def make_arinc429_word(instrument: Instrument, value: float) -> int:
	"""Crea una palabra demo de 32 bits con etiqueta, SDI, datos, SSM y paridad.

	El dato se escala linealmente a 19 bits sin signo. El formato real debe
	configurarse según el ICD específico (BNR/BCD, resolución, signo y SSM).
	"""
	if not math.isfinite(value) or not instrument.minimum <= value <= instrument.maximum:
		raise ValueError(
			f"El valor debe estar entre {instrument.minimum} y {instrument.maximum}."
		)
	if instrument.minimum >= instrument.maximum:
		raise ValueError("El rango del instrumento no es válido.")

	fraction = (value - instrument.minimum) / (
		instrument.maximum - instrument.minimum
	)
	data = round(fraction * MAX_DATA)
	word = reverse_label_bits(instrument.label) | (instrument.sdi << 8) | (data << 10)
	word |= 0b11 << 29  # SSM: funcionamiento normal, según este ejemplo.
	if word.bit_count() % 2 == 0:
		word |= 1 << 31
	return word


def play_tone(value: float, minimum: float, maximum: float) -> None:
	"""Emite en el dispositivo de audio un tono cuya frecuencia sigue el valor."""
	import numpy as np
	import sounddevice as sd

	fraction = (value - minimum) / (maximum - minimum)
	frequency = 220.0 + fraction * 660.0
	sample_rate = 44100
	duration = 0.35
	samples = np.arange(int(sample_rate * duration), dtype=np.float32)
	tone = 0.15 * np.sin(2 * np.pi * frequency * samples / sample_rate)
	sd.play(tone, samplerate=sample_rate, blocking=True)


def transmit_outputs(
	word: int,
	instrument: Instrument,
	value: float,
	serial_port: str,
	serial_enabled: bool,
	audio_enabled: bool,
) -> list[str]:
	"""Realiza en paralelo la salida serial de prueba y el tono de audio."""
	def serial_task() -> str:
		if not serial_enabled:
			return "Modo simulación: no se enviaron bytes por serial."
		import serial

		# Este framing genérico no representa una interfaz física ARINC 429.
		with serial.Serial(serial_port, baudrate=115200, timeout=1) as port:
			port.write(word.to_bytes(4, byteorder="big"))
			port.flush()
		return f"Palabra enviada a la interfaz serial de laboratorio {serial_port}."

	def audio_task() -> str:
		if not audio_enabled:
			return "Audio desactivado."
		play_tone(value, instrument.minimum, instrument.maximum)
		return "Tono reproducido."

	with ThreadPoolExecutor(max_workers=2) as executor:
		futures = [executor.submit(serial_task), executor.submit(audio_task)]
		results = []
		for future in futures:
			try:
				results.append(future.result())
			except Exception as exc:
				results.append(f"Salida no completada: {exc}")
	return results


class InstrumentDialog(tk.Toplevel):
	"""Diálogo modal para alta y edición de instrumentos."""

	def __init__(self, parent: tk.Misc, item: Instrument | None = None) -> None:
		super().__init__(parent)
		self.title("Editar instrumento" if item else "Nuevo instrumento")
		self.resizable(False, False)
		self.transient(parent)
		self.result: dict[str, object] | None = None
		self.variables = {
			"name": tk.StringVar(value=item.name if item else ""),
			"minimum": tk.StringVar(value=str(item.minimum) if item else ""),
			"maximum": tk.StringVar(value=str(item.maximum) if item else ""),
			"label": tk.StringVar(value=f"{item.label:03o}" if item else ""),
			"sdi": tk.StringVar(value=str(item.sdi) if item else "0"),
		}
		fields = (
			("Nombre", "name"),
			("Valor mínimo", "minimum"),
			("Valor máximo", "maximum"),
			("Etiqueta ARINC (octal)", "label"),
			("SDI (0–3)", "sdi"),
		)
		for row, (label, key) in enumerate(fields):
			ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=6)
			entry = ttk.Entry(self, textvariable=self.variables[key], width=30)
			entry.grid(row=row, column=1, padx=12, pady=6)
			if row == 0:
				entry.focus_set()
		buttons = ttk.Frame(self)
		buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", padx=12, pady=12)
		ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right", padx=(8, 0))
		ttk.Button(buttons, text="Guardar", command=self._save).pack(side="right")
		self.bind("<Return>", lambda _event: self._save())
		self.grab_set()

	def _save(self) -> None:
		try:
			name = self.variables["name"].get().strip()
			minimum = float(self.variables["minimum"].get())
			maximum = float(self.variables["maximum"].get())
			label = int(self.variables["label"].get().strip(), 8)
			sdi = int(self.variables["sdi"].get().strip())
			if not name or not math.isfinite(minimum) or not math.isfinite(maximum):
				raise ValueError("Completa el nombre y utiliza límites finitos.")
			if minimum >= maximum:
				raise ValueError("El valor mínimo debe ser menor al máximo.")
			if not 0 <= label <= 0o377 or not 0 <= sdi <= 3:
				raise ValueError("La etiqueta debe estar entre 000 y 377 octal; SDI entre 0 y 3.")
		except ValueError as exc:
			messagebox.showerror("Datos no válidos", str(exc), parent=self)
			return
		self.result = {
			"name": name,
			"minimum": minimum,
			"maximum": maximum,
			"label": label,
			"sdi": sdi,
		}
		self.destroy()


class ShipmentDialog(tk.Toplevel):
	"""Diálogo modal para alta y edición de envíos."""

	def __init__(
		self,
		parent: tk.Misc,
		instruments: dict[str, Instrument],
		shipment: Shipment | None = None,
	) -> None:
		super().__init__(parent)
		self.title("Editar envío" if shipment else "Nuevo envío")
		self.resizable(False, False)
		self.transient(parent)
		self.result: tuple[str, float] | None = None
		self.instruments = instruments
		self.by_display = {
			f"{item.name} [{item.id}]": item.id for item in instruments.values()
		}
		selected_display = next(
			(
				display
				for display, instrument_id in self.by_display.items()
				if shipment and instrument_id == shipment.instrument_id
			),
			next(iter(self.by_display), ""),
		)
		self.instrument_var = tk.StringVar(value=selected_display)
		self.value_var = tk.StringVar(value=str(shipment.value) if shipment else "")
		ttk.Label(self, text="Instrumento").grid(row=0, column=0, sticky="w", padx=12, pady=8)
		self.instrument_box = ttk.Combobox(
			self,
			textvariable=self.instrument_var,
			values=list(self.by_display),
			state="readonly",
			width=32,
		)
		self.instrument_box.grid(row=0, column=1, padx=12, pady=8)
		ttk.Label(self, text="Valor a transmitir").grid(row=1, column=0, sticky="w", padx=12, pady=8)
		value_entry = ttk.Entry(self, textvariable=self.value_var, width=35)
		value_entry.grid(row=1, column=1, padx=12, pady=8)
		buttons = ttk.Frame(self)
		buttons.grid(row=2, column=0, columnspan=2, sticky="e", padx=12, pady=12)
		ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right", padx=(8, 0))
		ttk.Button(buttons, text="Guardar", command=self._save).pack(side="right")
		value_entry.focus_set()
		self.bind("<Return>", lambda _event: self._save())
		self.grab_set()

	def _save(self) -> None:
		instrument_id = self.by_display.get(self.instrument_var.get())
		try:
			if instrument_id is None:
				raise ValueError("Selecciona un instrumento.")
			value = float(self.value_var.get())
			instrument = self.instruments[instrument_id]
			if not math.isfinite(value) or not instrument.minimum <= value <= instrument.maximum:
				raise ValueError(
					f"El valor debe estar entre {instrument.minimum} y {instrument.maximum}."
				)
		except ValueError as exc:
			messagebox.showerror("Datos no válidos", str(exc), parent=self)
			return
		self.result = (instrument_id, value)
		self.destroy()


class InstrumentShipmentApp:
	"""Ventana principal del CRUD."""

	def __init__(self, root: tk.Tk, data_path: Path) -> None:
		self.root = root
		self.store = JsonStore(data_path)
		self.executor = ThreadPoolExecutor(max_workers=1)
		self.root.title("Gestión ARINC 429 · Instrumentos y envíos")
		self.root.geometry("1050x720")
		self.root.minsize(850, 600)
		self.root.protocol("WM_DELETE_WINDOW", self._close)
		self._build_ui()
		self.refresh_all()

	def _build_ui(self) -> None:
		style = ttk.Style(self.root)
		if "clam" in style.theme_names():
			style.theme_use("clam")
		style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
		style.configure("Warning.TLabel", foreground="#8a3b12", wraplength=950)
		container = ttk.Frame(self.root, padding=18)
		container.pack(fill="both", expand=True)
		ttk.Label(container, text="Instrumentos y envíos", style="Title.TLabel").pack(anchor="w")
		ttk.Label(
			container,
			text=(
				"DEMOSTRACIÓN: escala ARINC simplificada. No conectar a aviónica. "
				"La salida serial solo debe utilizarse con una interfaz aislada de laboratorio."
			),
			style="Warning.TLabel",
		).pack(anchor="w", pady=(4, 14))

		self.tabs = ttk.Notebook(container)
		self.tabs.pack(fill="both", expand=True)
		self.instrument_tab = ttk.Frame(self.tabs, padding=12)
		self.shipment_tab = ttk.Frame(self.tabs, padding=12)
		self.tabs.add(self.instrument_tab, text="Instrumentos")
		self.tabs.add(self.shipment_tab, text="Envíos y transmisión")
		self._build_instruments_tab()
		self._build_shipments_tab()

	def _build_tree(self, parent: ttk.Frame, columns: tuple[str, ...]) -> ttk.Treeview:
		tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
		for column in columns:
			tree.heading(column, text=column)
			tree.column(column, width=130, anchor="center")
		tree.column(columns[0], width=95, anchor="center")
		tree.pack(fill="both", expand=True, pady=(0, 12))
		return tree

	def _build_instruments_tab(self) -> None:
		self.instrument_tree = self._build_tree(
			self.instrument_tab,
			("ID", "Nombre", "Mínimo", "Máximo", "Etiqueta octal", "SDI"),
		)
		buttons = ttk.Frame(self.instrument_tab)
		buttons.pack(fill="x")
		ttk.Button(buttons, text="Nuevo", command=self.create_instrument).pack(side="left")
		ttk.Button(buttons, text="Editar seleccionado", command=self.edit_instrument).pack(side="left", padx=8)
		ttk.Button(buttons, text="Eliminar seleccionado", command=self.delete_instrument).pack(side="left")
		ttk.Button(buttons, text="Actualizar lista", command=self.refresh_all).pack(side="right")

	def _build_shipments_tab(self) -> None:
		self.shipment_tree = self._build_tree(
			self.shipment_tab, ("ID", "Instrumento", "Valor", "Rango válido")
		)
		buttons = ttk.Frame(self.shipment_tab)
		buttons.pack(fill="x", pady=(0, 12))
		ttk.Button(buttons, text="Nuevo envío", command=self.create_shipment).pack(side="left")
		ttk.Button(buttons, text="Editar seleccionado", command=self.edit_shipment).pack(side="left", padx=8)
		ttk.Button(buttons, text="Eliminar seleccionado", command=self.delete_shipment).pack(side="left")

		output = ttk.LabelFrame(self.shipment_tab, text="Generar palabra y emitir", padding=12)
		output.pack(fill="x")
		self.serial_port_var = tk.StringVar()
		self.serial_enabled_var = tk.BooleanVar(value=False)
		self.audio_enabled_var = tk.BooleanVar(value=True)
		ttk.Label(output, text="Puerto serial:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
		ttk.Entry(output, textvariable=self.serial_port_var, width=24).grid(row=0, column=1, sticky="w", pady=5)
		ttk.Checkbutton(
			output,
			text="Habilitar transmisión a interfaz aislada de laboratorio",
			variable=self.serial_enabled_var,
		).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
		ttk.Checkbutton(
			output, text="Reproducir tono en auriculares", variable=self.audio_enabled_var
		).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
		self.transmit_button = ttk.Button(
			output, text="Generar y emitir envío seleccionado", command=self.send_selected_shipment
		)
		self.transmit_button.grid(row=0, column=2, rowspan=3, padx=18, sticky="ns")
		self.output_text = tk.Text(output, height=4, wrap="word", state="disabled")
		self.output_text.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
		output.columnconfigure(2, weight=1)

	def refresh_all(self) -> None:
		self._refresh_instruments()
		self._refresh_shipments()

	def _refresh_instruments(self) -> None:
		self.instrument_tree.delete(*self.instrument_tree.get_children())
		for item in self.store.instruments.values():
			self.instrument_tree.insert(
				"",
				"end",
				iid=item.id,
				values=(item.id, item.name, item.minimum, item.maximum, f"{item.label:03o}", item.sdi),
			)

	def _refresh_shipments(self) -> None:
		self.shipment_tree.delete(*self.shipment_tree.get_children())
		for shipment in self.store.shipments.values():
			instrument = self.store.instruments.get(shipment.instrument_id)
			name = instrument.name if instrument else "(inexistente)"
			range_text = (
				f"{instrument.minimum} – {instrument.maximum}" if instrument else "—"
			)
			self.shipment_tree.insert(
				"", "end", iid=shipment.id,
				values=(shipment.id, name, shipment.value, range_text),
			)

	def _selected(self, tree: ttk.Treeview, collection: dict) -> object | None:
		selection = tree.selection()
		if not selection:
			messagebox.showinfo("Selección requerida", "Selecciona un registro de la tabla.", parent=self.root)
			return None
		return collection.get(selection[0])

	def _save_store(self) -> bool:
		try:
			self.store.save()
			return True
		except OSError as exc:
			messagebox.showerror("Error de almacenamiento", str(exc), parent=self.root)
			return False

	def create_instrument(self) -> None:
		dialog = InstrumentDialog(self.root)
		self.root.wait_window(dialog)
		if dialog.result is None:
			return
		item = Instrument(id=new_id(), **dialog.result)
		self.store.instruments[item.id] = item
		if not self._save_store():
			del self.store.instruments[item.id]
			return
		self.refresh_all()

	def edit_instrument(self) -> None:
		item = self._selected(self.instrument_tree, self.store.instruments)
		if not isinstance(item, Instrument):
			return
		dialog = InstrumentDialog(self.root, item)
		self.root.wait_window(dialog)
		if dialog.result is None:
			return
		updated = Instrument(id=item.id, **dialog.result)
		if any(
			shipment.instrument_id == item.id
			and not updated.minimum <= shipment.value <= updated.maximum
			for shipment in self.store.shipments.values()
		):
			messagebox.showerror(
				"Rango incompatible",
				"El nuevo rango dejaría uno o más envíos fuera de límites.",
				parent=self.root,
			)
			return
		self.store.instruments[item.id] = updated
		if not self._save_store():
			self.store.instruments[item.id] = item
			return
		self.refresh_all()

	def delete_instrument(self) -> None:
		item = self._selected(self.instrument_tree, self.store.instruments)
		if not isinstance(item, Instrument):
			return
		if any(s.instrument_id == item.id for s in self.store.shipments.values()):
			messagebox.showerror(
				"Instrumento asociado", "Elimina primero sus envíos asociados.", parent=self.root
			)
			return
		if not messagebox.askyesno("Confirmar", f"¿Eliminar {item.name}?", parent=self.root):
			return
		del self.store.instruments[item.id]
		if not self._save_store():
			self.store.instruments[item.id] = item
			return
		self.refresh_all()

	def create_shipment(self) -> None:
		if not self.store.instruments:
			messagebox.showinfo("Sin instrumentos", "Crea primero un instrumento.", parent=self.root)
			return
		dialog = ShipmentDialog(self.root, self.store.instruments)
		self.root.wait_window(dialog)
		if dialog.result is None:
			return
		instrument_id, value = dialog.result
		shipment = Shipment(new_id(), instrument_id, value)
		self.store.shipments[shipment.id] = shipment
		if not self._save_store():
			del self.store.shipments[shipment.id]
			return
		self.refresh_all()

	def edit_shipment(self) -> None:
		shipment = self._selected(self.shipment_tree, self.store.shipments)
		if not isinstance(shipment, Shipment):
			return
		dialog = ShipmentDialog(self.root, self.store.instruments, shipment)
		self.root.wait_window(dialog)
		if dialog.result is None:
			return
		instrument_id, value = dialog.result
		self.store.shipments[shipment.id] = Shipment(shipment.id, instrument_id, value)
		if not self._save_store():
			self.store.shipments[shipment.id] = shipment
			return
		self.refresh_all()

	def delete_shipment(self) -> None:
		shipment = self._selected(self.shipment_tree, self.store.shipments)
		if not isinstance(shipment, Shipment):
			return
		if not messagebox.askyesno("Confirmar", "¿Eliminar el envío seleccionado?", parent=self.root):
			return
		del self.store.shipments[shipment.id]
		if not self._save_store():
			self.store.shipments[shipment.id] = shipment
			return
		self.refresh_all()

	def send_selected_shipment(self) -> None:
		shipment = self._selected(self.shipment_tree, self.store.shipments)
		if not isinstance(shipment, Shipment):
			return
		instrument = self.store.instruments.get(shipment.instrument_id)
		if instrument is None:
			messagebox.showerror("Instrumento inexistente", "El envío no tiene instrumento válido.", parent=self.root)
			return
		serial_enabled = self.serial_enabled_var.get()
		serial_port = self.serial_port_var.get().strip()
		if serial_enabled:
			if not serial_port:
				messagebox.showerror("Puerto requerido", "Indica el puerto serial de laboratorio.", parent=self.root)
				return
			confirmed = messagebox.askyesno(
				"Confirmar salida de laboratorio",
				"Confirma que el puerto está conectado únicamente a una interfaz aislada "
				"de laboratorio, no a aviónica ni a equipo operativo. ¿Continuar?",
				icon="warning",
				parent=self.root,
			)
			if not confirmed:
				return
		try:
			word = make_arinc429_word(instrument, shipment.value)
		except ValueError as exc:
			messagebox.showerror("Valor inválido", str(exc), parent=self.root)
			return
		self._set_output(
			f"Palabra 32 bits: {word:032b}\nHexadecimal: 0x{word:08X}\nProcesando salidas..."
		)
		self.transmit_button.configure(state="disabled")
		future = self.executor.submit(
			transmit_outputs,
			word,
			instrument,
			shipment.value,
			serial_port,
			serial_enabled,
			self.audio_enabled_var.get(),
		)
		future.add_done_callback(
			lambda completed: self.root.after(0, self._finish_transmission, completed, word)
		)

	def _finish_transmission(self, future: Future[list[str]], word: int) -> None:
		try:
			results = future.result()
			self._set_output(
				f"Palabra 32 bits: {word:032b}\nHexadecimal: 0x{word:08X}\n"
				+ "\n".join(results)
			)
		except Exception as exc:
			self._set_output(f"Error al emitir la palabra: {exc}")
		finally:
			self.transmit_button.configure(state="normal")

	def _set_output(self, text: str) -> None:
		self.output_text.configure(state="normal")
		self.output_text.delete("1.0", "end")
		self.output_text.insert("1.0", text)
		self.output_text.configure(state="disabled")

	def _close(self) -> None:
		self.executor.shutdown(wait=False, cancel_futures=True)
		self.root.destroy()


def main() -> None:
	data_path = Path(__file__).with_name("instrumentos_envios.json")
	root = tk.Tk()
	try:
		InstrumentShipmentApp(root, data_path)
	except (OSError, ValueError) as exc:
		messagebox.showerror("No se pudo iniciar", str(exc), parent=root)
		root.destroy()
		return
	root.mainloop()


if __name__ == "__main__":
	main()
