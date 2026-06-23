# Chaos Calculator Bullshit

<p align="center">
  <img src="media/chaos_logo_crt.png" alt="Chaos Calculator CRT logo" width="900">
</p>


Analizador de caos clásico con estética CRT retro/8-bit. Es una calculadora de sistemas dinámicos: le das un Hamiltoniano o un sistema de ecuaciones diferenciales ordinarias, integras, miras trayectorias, secciones, Lyapunov, SALI, animaciones 3D y figuras exportables.

Proyecto experimental con IA para probar ideas de automatización de analisis de caos clasico, con una interfaz ridículamente verde porque mola.

## Qué hace

- Integra sistemas Hamiltonianos y ODEs generales.
- Tiene presets clásicos de caos:
  - Salasnich / SU(2) Yang-Mills-Higgs homogéneo.
  - Canfora / Georgi-Glashow reducido.
  - Hénon-Heiles.
  - Three Body Figure-8.
  - Lorenz63.
  - Rössler.
  - Duffing forzado.
  - Chua circuit.
  - Péndulo doble.
- Genera condiciones iniciales aleatorias válidas de forma inteligente.
- Calcula FTLE, SALI y espectros de Lyapunov.
- Dibuja potenciales, trayectorias, proyecciones de fase, secciones de Poincaré y animaciones.
- Exporta figuras en `png`, `svg` y `pdf`.
- Guarda memoria de configuración en `data/config.json`.
- Reproduce música SID con `sidplayfp.exe`.

## Estructura

```text
ChaosCalculator/
├── ChaosCalculator.py              # launcher principal
├── ChaosCalculator_Motor.ipynb      # notebook para jugar con el motor sin la interfaz
├── README.md
├── requirements.txt
├── .gitignore
├── engine/
│   └── chaos_runtime.py             # motor + interfaz CRT
├── media/
│   ├── chaos_logo_crt.png
│   ├── I_Feel_Love.sid
│   ├── Ashes_to_Ashes.sid
│   └── sidplayfp.exe
└── data/
    └── .gitkeep                     # aquí se crean config, logs y figuras
```

Sí: está compacto a propósito. Más carpetas de las necesarias solo hacen que uno termine peleando con el proyecto en vez de estudiar el sistema dinámico.

## Instalación

```bash
pip install -r requirements.txt
python ChaosCalculator.py
```

En Windows, si usas terminal normal o Windows Terminal, debería funcionar bien. Si la música SID no suena, revisa que `media/sidplayfp.exe` esté presente.

## Uso rápido

Ejecuta:

```bash
python ChaosCalculator.py
```

Controles principales:

```text
↑/↓       mover selección
ENTER     ejecutar opción
ESC       volver
M         música ON/OFF
N         siguiente pista SID
B         pista SID anterior
```

La configuración se guarda automáticamente. Si cambias tema de Matplotlib, resolución, formatos de exportación, pista de música o perfiles de rendimiento, el programa lo recuerda.

## Perfiles de rendimiento

Hay perfiles para no hacer locuras con el notebook abierto, música sonando y una animación gigante guardándose en GIF:

- `SAFE / RAPIDO`: para probar sin esperar mucho.
- `NORMAL / EQUILIBRADO`: uso diario.
- `HIGH / PUBLICACION`: mejores figuras, más costo.
- `ULTRA / COSTO ALTO`: úsalo con criterio; puede demorarse bastante.

El programa avisa cuando una opción puede aumentar demasiado el costo computacional. No es decoración: algunos análisis de caos sí pueden crecer fuerte en tiempo de cómputo.

## Condiciones iniciales aleatorias inteligentes

No se trata de tirar números al azar y rezar. El motor intenta construir condiciones iniciales que tengan sentido para cada sistema:

- En Hamiltonianos, busca estados compatibles con regiones de energía razonables.
- En ODEs con cotas, usa cajas de condiciones iniciales y perturbaciones controladas.
- En N-cuerpos, evita cuerpos demasiado cerca, corrige centro de masa y momento total.
- Siempre descarta estados con `NaN`, `Inf`, derivadas absurdas o integraciones que explotan de inmediato.

Igual, esto no es magia. Si defines un sistema físicamente mal puesto, el programa no puede salvarlo todo.

## Notebook

El archivo:

```text
ChaosCalculator_Motor.ipynb
```

sirve para usar el motor sin la interfaz CRT. Ahí puedes:

- elegir un preset,
- cambiar parámetros,
- generar una condición inicial aleatoria,
- integrar,
- graficar,
- calcular Lyapunov/SALI,
- exportar datos.

Es la versión práctica para trabajar con Claude, depurar matemáticamente o hacer pruebas sin navegar menús.

## Música

Solo dejé dos temas SID:

- `I_Feel_Love.sid`
- `Ashes_to_Ashes.sid`

La música usa `sidplayfp.exe`. Si cierras el programa, el proceso de audio debería cerrarse también. Si algo queda colgado por cierre abrupto, el siguiente arranque intenta limpiar el proceso residual.

## Archivos generados

El programa genera cosas dentro de `data/`:

```text
data/config.json          # memoria del programa
data/logs/                # logs mínimos
data/figuras_caos/        # figuras, gifs, csv, etc.
```

Esos archivos están en `.gitignore`, porque no tiene sentido subir cada prueba al repositorio.

## Estado

Proyecto experimental, pero usable. La idea es ir ampliándolo como herramienta para analizar caos clásico en reducciones de modelos físicos, sistemas Hamiltonianos y ODEs no lineales arbitrarias.
