"""Sistema de procesamiento de archivos con menú interactivo Instalación: pip install rich questionary"""

import questionary

from FR.rutinas.setup import check_valid_entries
from FR.GCI import gci_folder
from FR.NDVI import ndvi_folder

from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from questionary import Style

console = Console()

# Estilo personalizado
estilo_custom = Style([
    ('qmark', 'fg:#673ab7 bold'),
    ('question', 'fg:#ffffff bold'),
    ('answer', 'fg:#00ff00 bold'),
    ('pointer', 'fg:#673ab7 bold'),
    ('highlighted', 'fg:#673ab7 bold'),
    ('selected', 'fg:#00ff00'),
    ('separator', 'fg:#cc5454'),
    ('instruction', 'fg:#858585'),
    ('text', 'fg:#ffffff'),
])

# Variables globales para las rutas
carpeta_entrada:str="data/INPUT"
carpeta_salida:str="data/OUTPUT"

def validar_carpeta(path):
    """Valida que la carpeta exista"""
    if not path:
        return "La ruta no puede estar vacía"
    
    path_obj = Path(path)
    if not path_obj.exists():
        return "La carpeta no existe"
    if not path_obj.is_dir():
        return "La ruta debe ser una carpeta"
    return True

def leer_archivos_entrada():
    """Lee los nombres de archivos en la carpeta de entrada"""
    if not carpeta_entrada:
        return []
    
    path_entrada = Path(carpeta_entrada)
    if not path_entrada.exists():
        return []
    
    try:
        archivos = [f.name for f in path_entrada.iterdir() if f.is_file()]
        return archivos
    except Exception as e:
        console.print(f"[red]Error al leer archivos: {e}[/red]")
        return []

def calcular_metricas_carpeta():
    """Calcula métricas de la carpeta de entrada"""
    if not carpeta_entrada:
        return None
    
    path_entrada = Path(carpeta_entrada)
    if not path_entrada.exists():
        return None
    
    try:
        archivos = list(path_entrada.iterdir())
        archivos_files = [f for f in archivos if f.is_file()]
        
        # Calcular métricas
        metricas = {
            'total_archivos': len(archivos_files),
            'tamano_total': sum(f.stat().st_size for f in archivos_files),
            'extensiones': {},
        }
        
        # Contar extensiones
        for archivo in archivos_files:
            ext = archivo.suffix.lower() or 'sin extensión'
            metricas['extensiones'][ext] = metricas['extensiones'].get(ext, 0) + 1
        
        return metricas
    
    except Exception as e:
        console.print(f"[red]Error al calcular métricas: {e}[/red]")
        return None

def formatear_tamano(bytes):
    """Formatea bytes a una unidad legible"""
    for unidad in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unidad}"
        bytes /= 1024.0
    return f"{bytes:.2f} TB"

def generar_subtitulo_metricas(metricas, mostrar_archivos=False):
    """Genera un subtítulo con métricas de la carpeta"""
    if not metricas:
        return "[red]No se encontraron archivos en la carpeta de entrada[/red]"
    
    lineas = []
    
    # Información básica
    lineas.append(f"[dim]Total de archivos: [bold]{metricas['total_archivos']}[/bold][/dim]")
    lineas.append(f"[dim]Tamaño total: [bold]{formatear_tamano(metricas['tamano_total'])}[/bold][/dim]")
    
    # Extensiones
    if metricas['extensiones']:
        ext_texto = ", ".join([f"{ext}: {count}" for ext, count in sorted(metricas['extensiones'].items())])
        lineas.append(f"[dim]Extensiones: {ext_texto}[/dim]")
    

    # Lista de archivos (opcional)
    if mostrar_archivos:
        archivos = leer_archivos_entrada()
        if archivos:
            lineas.append("\n[dim]Archivos:[/dim]")
            archivos_texto = "\n".join([f"  • {archivo}" for archivo in archivos[:10]])
            if len(archivos) > 10:
                archivos_texto += f"\n  [dim]... y {len(archivos) - 10} archivo(s) más[/dim]"
            lineas.append(archivos_texto)
    
    return "\n".join(lineas)

def mostrar_configuracion_actual():
    """Muestra la configuración actual en una tabla"""
    tabla = Table(title="⚙️ Configuración Actual", box=box.ROUNDED)
    tabla.add_column("Parámetro", style="cyan", no_wrap=True)
    tabla.add_column("Valor", style="green")
    
    entrada_text = carpeta_entrada if carpeta_entrada else "[red]No configurada[/red]"
    salida_text = carpeta_salida if carpeta_salida else "[red]No configurada[/red]"
    
    tabla.add_row("Carpeta de entrada", entrada_text)
    tabla.add_row("Carpeta de salida", salida_text)
    
    # Contar archivos si hay carpeta configurada
    if carpeta_entrada:
        archivos = leer_archivos_entrada()
        tabla.add_row("Archivos encontrados", str(len(archivos)))
    
    console.print(tabla)

def configuracion_inicial():
    """Menú de configuración inicial"""
    global carpeta_entrada, carpeta_salida
    
    while True:
        console.clear()
        console.print(Panel(
            "[bold cyan]Configura las carpetas de trabajo[/bold cyan]\n"
            "[dim]Ingresa las rutas completas de las carpetas[/dim]",
            title="📁 Configuración Inicial",
            border_style="cyan"
        ))
        
        mostrar_configuracion_actual()
        
        opcion = questionary.select(
            "\n¿Qué deseas hacer?",
            choices=[
                questionary.Choice("📂 Configurar carpeta de entrada", value="entrada"),
                questionary.Choice("📁 Configurar carpeta de salida", value="salida"),
                questionary.Separator(),
                questionary.Choice("✓ Continuar al menú principal", value="continuar"),
                questionary.Choice("← Volver", value="volver"),
            ],
            style=estilo_custom
        ).ask()
        
        if opcion == "volver" or opcion is None:
            return False
        
        if opcion == "continuar":
            if not carpeta_entrada or not carpeta_salida:
                console.print("\n[red]⚠ Debes configurar ambas carpetas antes de continuar[/red]")
                input("\nPresiona Enter para continuar...")
                continue
            return True
        
        if opcion == "entrada":
            ruta = questionary.path(
                "Ruta de la carpeta de entrada:",
                default=carpeta_entrada if carpeta_entrada else "./data/INPUT",
                validate=validar_carpeta,
                style=estilo_custom
            ).ask()
            
            if ruta:
                carpeta_entrada = ruta
                console.print(f"\n[green]✓ Carpeta de entrada configurada: {carpeta_entrada}[/green]")
                archivos = leer_archivos_entrada()
                console.print(f"[cyan]Se encontraron {len(archivos)} archivo(s)[/cyan]")
                # input("\nPresiona Enter para continuar...")
        
        elif opcion == "salida":
            ruta = questionary.path(
                "Ruta de la carpeta de salida:",
                default=carpeta_salida if carpeta_salida else "./data/OUTPUT",
                validate=validar_carpeta,
                style=estilo_custom
            ).ask()
            
            if ruta:
                carpeta_salida = ruta
                console.print(f"\n[green]✓ Carpeta de salida configurada: {carpeta_salida}[/green]")
                # input("\nPresiona Enter para continuar...")

def single_case_menu():
    """Menú Single Case con opciones de procesamiento"""
    
    while True:
        console.clear()
        
        # Calcular métricas
        metricas = calcular_metricas_carpeta()
        subtitulo = generar_subtitulo_metricas(metricas, mostrar_archivos=False)
        
        console.print(Panel(
            f"[bold green]Procesamiento de casos individuales[/bold green]\n\n{subtitulo}",
            title="🔧 Single Case",
            border_style="green"
        ))
        
        opcion = questionary.select(
            "\nSelecciona una opción de procesamiento:",
            choices=[
                questionary.Choice("1️⃣  Opción 1: GCI", value="opcion1"),
                questionary.Choice("2️⃣  Opción 2: NDVI", value="opcion2"),
                questionary.Choice("3️⃣  Opción 3: NDMI", value="opcion3"),
                questionary.Separator("─" * 50),
                questionary.Choice("⚙️  Reconfigurar carpetas", value="config"),
                questionary.Choice("← Volver al menú principal", value="volver"),
            ],
            style=estilo_custom,
            instruction="(Usa las flechas para navegar)"
        ).ask()
        
        if opcion == "volver" or opcion is None:
            break
        
        if opcion == "config":
            configuracion_inicial()
            continue
        
        # Verificar que haya archivos antes de procesar
        if not metricas or metricas['total_archivos'] == 0:
            console.print("\n[red]⚠ No hay archivos para procesar en la carpeta de entrada[/red]")
            input("\nPresiona Enter para continuar...")
            continue
        
        # Procesar según la opción seleccionada
        console.clear()

        def band_calc_info(inputs:list[str]) :
            valids,_=check_valid_entries(inputs,input_folder=carpeta_entrada) #type : ignore

            time_intervals=[f' {v.fecha_inicio}  -->  {v.fecha_fin}' for v in valids]

            console.print(Panel(
                f"[cyan]Bandas de trabajo : {inputs}\n[/cyan]"
                f"Detectadas {len(valids)} instancias temporales  ",
            ))
            
            intervals_chosen = (questionary.checkbox(
            " Selecciona una opción de procesamiento : \n",
            choices=[questionary.Choice(title=name,value=id) for id,name in  enumerate(time_intervals)],
            style=estilo_custom,
            instruction="(Usa las flechas para navegar y espacio para seleccionar)"
            ).ask())

            return intervals_chosen

        
        if opcion == "opcion1":
            
            inputs=["B03","B08"]

            chosen_intervals_idxs = band_calc_info(inputs)
            gci_folder(input_folder=carpeta_entrada,output_folder=carpeta_salida,indices=chosen_intervals_idxs,export_image=True)

        
        elif opcion == "opcion2":
            inputs=["B04","B08"]

            chosen_intervals_idxs = band_calc_info(inputs)
            ndvi_folder(input_folder=carpeta_entrada,output_folder=carpeta_salida,indices=chosen_intervals_idxs,export_image=True)
        
        elif opcion == "opcion3":
            inputs=["B08","B11"]

            chosen_intervals_idxs = band_calc_info(inputs)
        
        
        # console.print("\n[green]✓ Proceso completado exitosamente![/green]")
        input("\nPresiona Enter para volver al menú...")

def temporal_case_menu():
    """Menú Temporal Case con opciones de procesamiento"""
    
    while True:
        console.clear()
        
        # Calcular métricas
        metricas = calcular_metricas_carpeta()
        subtitulo = generar_subtitulo_metricas(metricas, mostrar_archivos=True)
        
        console.print(Panel(
            f"[bold magenta]Procesamiento temporal de casos[/bold magenta]\n\n{subtitulo}",
            title="⏱️ Temporal Case",
            border_style="magenta"
        ))
        
        opcion = questionary.select(
            "\nSelecciona una opción de procesamiento temporal:",
            choices=[
                questionary.Choice("1️⃣  Opción 1: Análisis temporal", value="opcion1"),
                questionary.Choice("2️⃣  Opción 2: Series de tiempo", value="opcion2"),
                questionary.Choice("3️⃣  Opción 3: Tendencias", value="opcion3"),
                questionary.Choice("4️⃣  Opción 4: Predicciones", value="opcion4"),
                questionary.Choice("5️⃣  Opción 5: Comparativa temporal", value="opcion5"),
                questionary.Separator("─" * 50),
                questionary.Choice("⚙️  Reconfigurar carpetas", value="config"),
                questionary.Choice("← Volver al menú principal", value="volver"),
            ],
            style=estilo_custom,
            instruction="(Usa las flechas para navegar)"
        ).ask()
        
        if opcion == "volver" or opcion is None:
            break
        
        if opcion == "config":
            configuracion_inicial()
            continue
        
        # Verificar que haya archivos antes de procesar
        if not metricas or metricas['total_archivos'] == 0:
            console.print("\n[red]⚠ No hay archivos para procesar en la carpeta de entrada[/red]")
            input("\nPresiona Enter para continuar...")
            continue
        
        # Procesar según la opción seleccionada
        console.clear()
        
        if opcion == "opcion1":
            console.print(Panel(
                f"[magenta]Analizando patrones temporales en {metricas['total_archivos']} archivo(s)...\n\n"
                f"Carpeta entrada: {carpeta_entrada}\n"
                f"Carpeta salida: {carpeta_salida}\n\n"
                f"Procesando datos temporales...[/magenta]",
                title="1️⃣ Análisis temporal"
            ))
        
        elif opcion == "opcion2":
            console.print(Panel(
                f"[magenta]Procesando series de tiempo de {metricas['total_archivos']} archivo(s)...[/magenta]",
                title="2️⃣ Series de tiempo"
            ))
        
        elif opcion == "opcion3":
            console.print(Panel(
                f"[magenta]Analizando tendencias en {metricas['total_archivos']} archivo(s)...[/magenta]",
                title="3️⃣ Tendencias"
            ))
        
        elif opcion == "opcion4":
            console.print(Panel(
                f"[magenta]Generando predicciones de {metricas['total_archivos']} archivo(s)...[/magenta]",
                title="4️⃣ Predicciones"
            ))
        
        elif opcion == "opcion5":
            console.print(Panel(
                f"[magenta]Realizando comparativa temporal de {metricas['total_archivos']} archivo(s)...[/magenta]",
                title="5️⃣ Comparativa temporal"
            ))
        
        console.print("\n[green]✓ Proceso temporal completado exitosamente![/green]")
        input("\nPresiona Enter para volver al menú...")

def menu_principal():
    """Menú principal del sistema"""
    
    # Primero ejecutar configuración inicial
    if not configuracion_inicial():
        console.print("\nConfiguración cancelada. Saliendo...\n")
        return
    
    while True:
        console.clear()
        console.print(Panel(
            "[bold green]Sistema de Procesamiento de Archivos[/bold green]\n"
            "[dim]Usa las flechas ↑↓ para navegar y Enter para seleccionar[/dim]",
            title="🎯 Menú Principal",
            border_style="green"
        ))
        
        mostrar_configuracion_actual()
        
        opcion = questionary.select(
            "\n¿Qué deseas hacer?",
            choices=[
                questionary.Choice("🔧  Single Case", value="single"),
                questionary.Choice("⏱️  Temporal Case", value="temporal"),
                questionary.Choice("⚙️  Reconfigurar carpetas", value="config"),
                questionary.Separator("─" * 50),
                questionary.Choice("🚪 Salir", value="salir"),
            ],
            style=estilo_custom
        ).ask()
        
        if opcion is None or opcion == "salir":
            if opcion == "salir":
                confirmar = questionary.confirm(
                    "¿Estás seguro de que deseas salir?",
                    default=False,
                    style=estilo_custom
                ).ask()
                
                if confirmar:
                    console.print("\n[bold red]👋 ¡Hasta luego![/bold red]\n")
                    break
            else:
                # Ctrl+C en menu_principal - propagar
                raise KeyboardInterrupt()
        
        elif opcion == "single":
            single_case_menu()
        
        elif opcion == "temporal":
            temporal_case_menu()
        
        elif opcion == "config":
            configuracion_inicial()


if __name__ == "__main__":
    try:
        menu_principal()
    except KeyboardInterrupt:
        console.print("\n\n[bold red]👋 Programa interrumpido. ¡Hasta luego![/bold red]\n")
