# CompressOS Studio

Estación audiovisual local para escribir y producir videos con Stable Diffusion en Linux.

## Arranque en Linux

1. Instala Python 3.12, FFmpeg (incluido ffprobe), el controlador Vulkan de AMD y `vulkan-tools`. Comprueba la GPU con `vulkaninfo --summary`.
2. Instala [Stable Diffusion.cpp para Linux con Vulkan](https://github.com/leejet/stable-diffusion.cpp/releases/tag/master-1133-52491bd), o compílalo siguiendo [las instrucciones de Vulkan](https://github.com/leejet/stable-diffusion.cpp/blob/52491bd/docs/build.md). La compilación usa `-DSD_VULKAN=ON`, `glslc` y las cabeceras Vulkan/SPIR-V. Usa el binario de la distribución compatible y confirma el dispositivo AMD con `sd-cli --list-devices`.
3. Crea el entorno virtual con `python3 -m venv .venv`, actívalo con `source .venv/bin/activate` e instala las dependencias con `pip install -r requirements-linux.txt`.
4. Copia `.env.example` a `.env` y dale permisos con `chmod 600 .env`. Indica el binario, el dispositivo Vulkan y las rutas a los checkpoints de los perfiles que usarás. Cada perfil espera un checkpoint completo en un solo archivo. Para personajes, configura además un [IP-Adapter Plus SDXL con su codificador CLIP Vision ViT-H](https://github.com/leejet/stable-diffusion.cpp/blob/52491bd/docs/models/ip_adapter.md). La estación usa un flujo DGL que selecciona una referencia por inferencia. Las pruebas de tres poses alternan las fotos del personaje.
5. En el proyecto Supabase seleccionado, confirma un usuario con correo y contraseña en **Authentication → Users**. Configura `SUPABASE_PUBLISHABLE_KEY` y autoriza el correo en `COMPRESSO_AUTH_EMAILS`. Todos los correos autorizados comparten los datos de esta estación. Activa `COMPRESSO_LOCAL_DEMO=1` para abrir una copia local de prueba sin login.
6. Ejecuta `python -m app.linux`. La estación escucha en `http://127.0.0.1:8765`. Para cambiar el puerto agrega `--puerto 8780`.

El modo demo usa un directorio independiente cuando no se indica `COMPRESSO_DATA_DIR`. El perfil de producción guarda los datos en `$XDG_DATA_HOME/compresso-studio` o `~/.local/share/compresso-studio`. Copia ahí tus carpetas `guiones`, `salida`, `assets` y tus ajustes compatibles para retomar proyectos. Los MP3 originales se agregan a las categorías vacías al iniciar.

## Proveedores de guion

- Gemini usa `GEMINI_API_KEY` y puede investigar con Google Search cuando el modelo, la cuenta y la cuota lo permiten. Google Drive requiere autorización independiente para sus archivos.
- OpenAI usa `OPENAI_API_KEY`. El acceso a la API se administra desde OpenAI Platform.
- Ollama usa un modelo ya instalado en el servidor local y trabaja con el material que proporciones.
- Para guardar claves desde Ajustes, genera una clave Fernet con `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` y guárdala como `COMPRESSO_CREDENTIAL_KEY` en `.env`. Si migras de Windows y hay claves DPAPI, la pantalla de recuperación permite reemplazarlas con una clave de API vigente.

## Vulkan y shaders

El backend y el dispositivo se seleccionan en el proceso nativo. GGUF es el formato del checkpoint cuantizado. Los shaders se compilan con la revisión de GGML que fija Stable Diffusion.cpp. Para actualizar, compila y prueba la revisión completa con el dispositivo AMD antes de sustituir el binario. El selector de calidad controla el modelo, y el ajuste automático de convolución y Flash Attention del motor decide qué kernels usar según el dispositivo. [Selección de backend y kernels](https://github.com/leejet/stable-diffusion.cpp/blob/52491bd/docs/guides/backend.md)
