# SOLOServis
# Plataforma de de Productos y Servicios

Plataforma web orientada a la **búsqueda, comparación y consulta de productos y servicios**, permitiendo a los usuarios encontrar diferentes alternativas y comparar información relevante entre ellas.

El proyecto toma como referencia el concepto de plataformas como SoloTodo, pero amplía el enfoque para incorporar tanto **productos como servicios**.

---

## 📌 Objetivo del proyecto

Desarrollar una plataforma que permita centralizar información de productos y servicios provenientes de diferentes fuentes, facilitando al usuario:

* Buscar productos y servicios.
* Filtrar resultados.
* Comparar diferentes alternativas.
* Consultar precios.
* Consultar tiendas o proveedores.
* Revisar información relevante de cada alternativa.
* Facilitar la toma de decisiones de compra o contratación.

La arquitectura estará diseñada para permitir incorporar nuevas categorías y funcionalidades a medida que evolucione el proyecto.

---

# 🏗️ Arquitectura

El sistema utilizará una arquitectura separada por capas:

| Área                       | Tecnología                  |
| -------------------------- | --------------------------- |
| Frontend                   | React                       |
| Lenguaje Frontend          | TypeScript                  |
| Build Tool                 | Vite                        |
| Gestión de consultas       | TanStack Query              |
| Backend                    | Go                          |
| Router HTTP                | Chi                         |
| Base de datos              | PostgreSQL                  |
| Driver PostgreSQL          | pgx                         |
| Generación de código SQL   | sqlc                        |
| Caché                      | Redis                       |
| Scraping                   | Python                      |
| Framework de scraping      | Scrapling                   |
| Automatización web         | Playwright                  |
| Procesamiento de datos     | Go Workers                  |
| Almacenamiento RAW inicial | JSONL                       |
| Contenedores               | Docker                      |
| Orquestación local         | Docker Compose              |
| IA/NLP                     | Pendiente de implementación |

```text
┌─────────────────────────────────────┐
│              FRONTEND               │
│        React + TypeScript + Vite    │
└──────────────────┬──────────────────┘
                   │
                   │ HTTP / REST
                   ▼
┌─────────────────────────────────────┐
│               BACKEND               │
│  GO- CHi - redis - python- Dockers│
└──────────────────┬──────────────────┘
                   │
                   │ Prisma
                   ▼
┌─────────────────────────────────────┐
│             DATABASE                │
│              PostgreSQL             │
└─────────────────────────────────────┘
```

La infraestructura de desarrollo utilizará Docker y Docker Compose.

---

# 🛠️ Tecnologías

## Frontend

### React

Utilizado para construir la interfaz mediante componentes reutilizables.

### TypeScript

Utilizado para mantener tipado estático y mejorar la mantenibilidad del proyecto.

### Vite

Utilizado como herramienta de desarrollo y build del frontend debido a su rapidez y configuración sencilla.


Instlacion :
  Primero instalar Node.JS 
https://nodejs.org/es

  Una ves instalado correr DENTRO DE LA CARPETA FRONTED *(npm install)*,  en caso de que no responda el comando una ves instalado Node , reiniciar equipo y verificar nuevamente con el mismo comando.
  luego ingresar *(npm run dev)* y aparecerá el link de local host en la terminal para poder abrirlo ingresar *(CTRL + clik izquierdo)*
---

## Backend

### Node.js

Runtime utilizado para ejecutar el backend.

### NestJS

Framework utilizado para estructurar el backend mediante módulos, controladores y servicios.

### REST

La comunicación entre frontend y backend se realizará mediante una API REST.

---

## Base de datos

### PostgreSQL

Base de datos relacional principal del sistema.

Se utilizará debido a la cantidad de relaciones existentes entre entidades como:

```text
Usuarios
Productos
Servicios
Categorías
Tiendas
Proveedores
Precios
Historial de precios
Favoritos
```

### Prisma

ORM utilizado para conectar el backend con PostgreSQL y administrar el esquema y las migraciones.

---

## Infraestructura

### Docker

Utilizado para mantener un entorno de desarrollo reproducible entre los integrantes del equipo.

### Docker Compose

Utilizado inicialmente para administrar los servicios locales, principalmente PostgreSQL.

---


## Calidad de código

### ESLint

Utilizado para detectar errores y mantener reglas de código consistentes.

### Prettier

Utilizado para mantener un formato de código uniforme entre todos 
---

## CI


## Documentación de API

### Swagger / OpenAPI

La API contará con documentación mediante Swagger para facilitar el trabajo entre frontend y backend.

La documentación permitirá consultar:

* Endpoints.
* Parámetros.
* Respuestas.
* Modelos.
* Métodos HTTP.

---

# 📂 Estructura del proyecto

La estructura inicial será:

```text
project/
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── services/
│   │   ├── hooks/
│   │   ├── types/
│   │   └── assets/
│   │
│   ├── public/
│   └── package.json
│
├── backend/
│   ├── src/
│   │   ├── modules/
│   │   ├── common/
│   │   ├── config/
│   │   └── main.ts
│   │
│   ├── prisma/
│   │   └── schema.prisma
│   │
│   └── package.json
│
├── docs/
│
├── docker/
│
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

#IMPORTANTE
DOCS : tendrá que integrar una estructura similar a foonted y backend guardando diferenciado por documentos que poseen la DOCUMENTACION nesesaria apra la comprencion del el PQ y PARA Q de la funcion a realisa y si depende o dependen otras de esta.
#IMPORTANTE

La estructura se modificara a medida que el proyecto crezca.

---

# 🔄 Flujo de desarrollo

El desarrollo seguirá el siguiente flujo:

```text
Trello
   ↓
Task
   ↓
Git Branch
   ↓
Development
   ↓
Pull Request
   ↓
Code Review
   ↓
Tests
   ↓
Merge
   ↓
DONE
```

Cada funcionalidad deberá estar asociada a una tarea de Trello.

---

# 🌿 Estrategia Git

Se utilizará una estrategia basada en ramas.

```text
main
│
└── develop
     │
     ├── feature/*
     ├── fix/*
     └── refactor/*
```

## `main`

Contendrá versiones estables del proyecto.

No se deberá desarrollar directamente sobre esta rama.

## `develop`

Contendrá la integración del desarrollo actual.

## `feature/*`

Utilizada para desarrollar nuevas funcionalidades.

# 💬 Convención de commits

Los commits deberán utilizar una estructura clara explicando el motivo de la implementacion.


# 🔍 Pull Requests

Toda funcionalidad deberá integrarse mediante Pull Request.

Una Pull Request deberá:

* Tener un título descriptivo.
* Indicar qué problema resuelve.
* Indicar los cambios realizados.
* Relacionarse con una tarea de Trello.
* Pasar los tests.
* Pasar ESLint.
* Ser revisada por otro integrante.

## Regla de Code Review

El desarrollador que implementó una funcionalidad **no deberá aprobar su propia Pull Request**.

El código deberá ser revisado por al menos otros integrante.

---

# ✅ Definition of Done

Una tarea podrá considerarse terminada cuando:

```text
[ ] Funcionalidad implementada
[ ] Código revisado
[ ] Code Review aprobado
[ ] Tests realizados
[ ] Tests aprobados
[ ] ESLint sin errores
[ ] Build exitoso
[ ] Criterios de aceptación cumplidos
[ ] Pull Request integrada
[ ] Tarjeta de Trello actualizada
```

---


# 🔐 Variables de entorno

Las configuraciones sensibles deberán mantenerse mediante variables de entorno.

Ejemplo:

```env
DATABASE_URL=
API_URL=
PORT=
```

El archivo:

```text
.env
```

no deberá subirse al repositorio.

Se proporcionará:

```text
.env.example
```

como referencia para configurar el entorno local.

---

# 🚀 Instalación

## Requisitos

Antes de comenzar se deberá contar con:

* Git
* Node.js
* npm
* Docker
* Docker Compose

---

## Clonar el repositorio (por crear)

```bash
git clone <REPOSITORY_URL>
cd <PROJECT_NAME>
```

---
## Instalar dependencias

Frontend:

```bash
cd frontend
npm install
```

Backend:

```bash
cd ../backend
npm install
```

---

## Ejecutar backend

```bash
cd backend
npm run start:dev
```

---

## Ejecutar frontend

En otra terminal:

```bash
cd frontend
npm run dev
```

---


# 📋 Gestión del proyecto

La gestión del proyecto se realizará mediante Trello.
El creacion de funciones y seguir la estructura de carpetas debe d ser documentado



# 👥 Equipo

El proyecto será desarrollado por un equipo de 6 integrantes.

Las responsabilidades se distribuirán durante la planificación de cada Sprint.

La asignación de tareas se realizará mediante Trello.

---

# 📈 Escalabilidad (por ver)


# 🚫 Tecnologías excluidas

Por las restricciones del proyecto:

```text
PHP
Java
```

No forman parte del stack.
---

# 🎯 Estado del proyecto

Actualmente el proyecto se encuentra en fase de planificación y configuración inicial.

## Próximos objetivos

```text
1. Definir arquitectura
2. Configurar repositorio
3. Configurar entorno de desarrollo
4. Configurar frontend
5. Configurar backend
6. Configurar PostgreSQL
7. Configurar Docker
8. Configurar CI
9. Crear primera integración Frontend → Backend → Database
10. Comenzar desarrollo de funcionalidades
```

---
