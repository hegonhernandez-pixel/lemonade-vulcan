# Reglas para escribir un guion

Videos de historias reales, mentes brillantes, misterios y películas, contados con narración y una
imagen generada por IA en cada escena.

## Cada escena

- `texto`: lo que dice la voz en esa escena. Español neutro latino, frases cortas, pensadas para leerse
  en voz alta. Una escena = una idea = 1 a 3 frases (4-12 segundos de audio). Nunca más de 40 palabras
  por escena. **Puntúa bien:** los subtítulos cortan sus grupos en `. , ; : ? !`, así que una coma bien
  puesta también es un buen corte en pantalla. Evita frases larguísimas sin puntuación.
- `prompt`: descripción en inglés de la imagen de esa escena: lugar, época, luz, objeto o acción. Sin
  texto dentro de la imagen y sin nombres de personas reales (describe "a scientist in his 50s with a
  beard", no su nombre). El estilo visual se añade solo a cada prompt: no lo repitas. Evita manos y caras
  en primer plano, y no pongas más de 2 personas en una imagen. **Corto:** el modelo de imagen solo lee
  unas 75 piezas de texto (tokens) contando el estilo visual y, con `@nombre`, la descripción del
  personaje; lo que pasa de ahí se corta y no sale en la imagen. Máximo **25 palabras** por prompt, y
  **15 palabras** después de `@nombre` cuando hay personaje. Lo más importante va primero.
- **Personajes (`@nombre`)**: si te indican que una persona es un personaje (p. ej. "usa a @lucia"),
  escribe `@nombre` al **inicio** del `prompt` de cada escena donde aparece, y luego la acción y el
  lugar: `"@lucia reading a letter by a window, 1950s kitchen, morning light"`. La cara sale de sus
  fotos de referencia, así que **no describas su físico**, solo la escena. Un solo `@` por escena. No
  inventes personajes que no te hayan dado. Las escenas sin esa persona (lugares, objetos, multitudes) van
  sin `@`. Mejor planos medios o primeros planos: en planos generales la cara sale pequeña.
- `pausa` (opcional): segundos de silencio después de la voz (0,5-1 s), solo tras una frase fuerte que
  necesita respirar: el giro o el dato que pega. Nunca en todas las escenas.

## Título y miniatura

- `titulo`: el título del video.
- `titulo_miniatura`: **siempre propónlo.** Máximo 6 palabras (se ve en 2 líneas, en mayúsculas, sobre
  la miniatura). Marca entre asteriscos la palabra clave que va resaltada: `"El científico que *desapareció*"`.
  No repitas el título: es el gancho visual (la contradicción, el dato raro, la pregunta).

## Extensión

- Short (9:16, 45-60 s): 100 a 140 palabras en total.
- Largo (16:9, 8-12 min): 1.200 a 1.800 palabras.

## Lo que hace que un video funcione

1. **Gancho en la primera frase.** Nada de "hoy vamos a hablar de". Empieza con el dato más raro, la
   pregunta o la contradicción. Ejemplo: "Nadie sabe dónde está el cuerpo de este hombre."
2. **Estructura de tensión:** gancho → contexto mínimo → giro → consecuencia → cierre abierto.
3. **Hechos verificables.** Fechas, lugares, nombres. Si no estás seguro, no lo inventes: di "según
   varios testimonios" o deja un `// VERIFICAR` en el texto para que se revise.
4. **Cierre con una pregunta o una idea que se queda.** Los Shorts terminan con un gancho hacia el canal
   ("La historia completa está en el canal"){{FRASE_CIERRE}} Los largos terminan con la idea central reformulada.
5. **Original siempre.** El guion es propio. Los clips de terceros, si se usan, son de 5-10 segundos
   como cita dentro de la narración, nunca el video completo.
6. Sin palabras en inglés innecesarias, sin "en este video", sin "sin más preámbulos".
