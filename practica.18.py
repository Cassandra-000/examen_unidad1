import os
import random
from datetime import datetime, timezone
from urllib.parse import quote_plus

from bson.objectid import ObjectId
from dotenv import load_dotenv
import pandas as pd
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import streamlit as st

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# ==============================================================================
# REPOSITORIO MONGO ATLAS
# ==============================================================================
class MongoAtlasRepository:
    def __init__(self):
        self.mongo_user = os.getenv("mongo_user")
        self.mongo_password = os.getenv("mongo_password")
        self.mongo_closter = os.getenv("mongo_closter")
        self.db_name = os.getenv("mongo_db", "sensor_db")
        self.collection_name = os.getenv("mongo_collection", "gato")
        self.client = None
        self.collection = None
        self.error = None

        if not self.mongo_user or not self.mongo_password or not self.mongo_closter:
            self.error = "Faltan mongo_user, mongo_password o mongo_closter"
            return

        uri = (
            f"mongodb+srv://{self.mongo_user}:{quote_plus(self.mongo_password)}@{self.mongo_closter}/"
            "?retryWrites=true&w=majority"
        )

        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command("ping")
            self.collection = self.client[self.db_name][self.collection_name]
        except (PyMongoError, Exception) as exc:
            self.error = f"No se pudo conectar a Mongo Atlas: {exc}"
            self.client = None
            self.collection = None

    def guardar_medicion(self, temperatura, humedad, accion):
        if self.collection is None:
            return False, self.error or "Sin conexión a Mongo Atlas."

        documento = {
            "temperatura_c": float(temperatura),
            "humedad_pct": float(humedad),
            "accion": accion,
            "fecha": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = self.collection.insert_one(documento)
            return True, f"Guardado con id: {result.inserted_id}"
        except PyMongoError as exc:
            return False, f"Error al guardar: {exc}"

    def obtener_todos(self):
        if self.collection is None:
            return False, self.error or "Sin conexión a Mongo Atlas."
        try:
            documentos = list(self.collection.find())
            return True, documentos
        except PyMongoError as exc:
            return False, f"Error al consultar: {exc}"

    def obtener_por_id(self, doc_id):
        if self.collection is None:
            return False, self.error or "Sin conexión a Mongo Atlas."
        try:
            documento = self.collection.find_one({"_id": ObjectId(doc_id)})
            if documento:
                return True, documento
            return False, "No se encontró ningún documento con ese ID."
        except Exception as exc:
            return False, f"ID inválido o error: {exc}"

    def actualizar_medicion(self, doc_id, temperatura, humedad, accion):
        if self.collection is None:
            return False, self.error or "Sin conexión a Mongo Atlas."
        try:
            filtro = {"_id": ObjectId(doc_id)}
            nuevos_valores = {
                "$set": {
                    "temperatura_c": float(temperatura),
                    "humedad_pct": float(humedad),
                    "accion": accion,
                    "fecha_actualizacion": datetime.now(timezone.utc).isoformat(),
                }
            }
            resultado = self.collection.update_one(filtro, nuevos_valores)
            if resultado.modified_count > 0:
                return True, "Documento actualizado correctamente."
            return False, "No se realizó ningún cambio o ID no encontrado."
        except Exception as exc:
            return False, f"Error al actualizar: {exc}"

    def eliminar_medicion(self, doc_id):
        if self.collection is None:
            return False, self.error or "Sin conexión a Mongo Atlas."
        try:
            resultado = self.collection.delete_one({"_id": ObjectId(doc_id)})
            if resultado.deleted_count > 0:
                return True, "Documento eliminado correctamente."
            return False, "No se encontró ningún documento para eliminar."
        except Exception as exc:
            return False, f"Error al eliminar: {exc}"


# ==============================================================================
# CLASE DEL AGENTE
# ==============================================================================
class AgenteClimatizacion:
    def __init__(self):
        self.temperatura = 0.0
        self.humedad = 0.0
        self.accion = ""
        self.mongo = MongoAtlasRepository()

    def percibir(self, temperatura, humedad):
        self.temperatura = temperatura
        self.humedad = humedad

    def tomar_decision(self):
        if self.temperatura > 30 and self.humedad > 70:
            self.accion = "Encender aire acondicionado"
        elif self.temperatura > 30:
            self.accion = "Encender ventilador"
        elif self.temperatura < 18:
            self.accion = "Encender calefacción"
        else:
            self.accion = "Mantener sistema apagado"

    def simular_por_accion(self, accion_deseada):
        if accion_deseada == "Encender aire acondicionado":
            self.temperatura = round(random.uniform(31.0, 42.0), 1)
            self.humedad = round(random.uniform(71.0, 95.0), 1)
        elif accion_deseada == "Encender ventilador":
            self.temperatura = round(random.uniform(31.0, 40.0), 1)
            self.humedad = round(random.uniform(20.0, 69.0), 1)
        elif accion_deseada == "Encender calefacción":
            self.temperatura = round(random.uniform(5.0, 17.9), 1)
            self.humedad = round(random.uniform(30.0, 80.0), 1)
        else:
            self.temperatura = round(random.uniform(18.0, 30.0), 1)
            self.humedad = round(random.uniform(30.0, 60.0), 1)

        self.accion = accion_deseada
        return self.temperatura, self.humedad

    def guardar_en_mongo(self):
        return self.mongo.guardar_medicion(
            self.temperatura, self.humedad, self.accion
        )


agente = AgenteClimatizacion()

# ==============================================================================
# INTERFAZ DE STREAMLIT (Navegador Web)
# ==============================================================================
st.set_page_config(page_title="Agente de Climatización", layout="wide")

st.title("🌡️ Control de Climatización - MongoDB Atlas")

col_izquierda, col_derecha = st.columns([1, 1.2])

with col_izquierda:
    st.subheader("⚙️ Opciones de Simulación y CRUD")

    tab1, tab2, tab3 = st.tabs(["🎯 Por Acción", "📝 Manual", "🔧 CRUD por ID"])

    with tab1:
        st.write("Selecciona una acción:")
        accion_sel = st.selectbox(
            "Acción a ejecutar:",
            [
                "Encender ventilador",
                "Encender aire acondicionado",
                "Encender calefacción",
                "Mantener sistema apagado",
            ]
        )

        if st.button("Simular y Guardar en Mongo", type="primary"):
            t_gen, h_gen = agente.simular_por_accion(accion_sel)
            exito, msj = agente.guardar_en_mongo()
            if exito:
                st.success(f"Guardado exitosamente: Temp={t_gen}°C, Hum={h_gen}%")
            else:
                st.error(msj)

    with tab2:
        t_input = st.number_input("Temperatura (°C):", value=25.0, step=0.5)
        h_input = st.number_input("Humedad (%):", value=50.0, min_value=0.0, max_value=100.0)

        if st.button("Evaluar y Guardar"):
            agente.percibir(t_input, h_input)
            agente.tomar_decision()
            exito, msj = agente.guardar_en_mongo()
            if exito:
                st.success(f"Acción asignada: {agente.accion}")
            else:
                st.error(msj)

    with tab3:
        doc_id = st.text_input("ID del documento en Mongo:")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Buscar"):
                if doc_id:
                    exito, doc = agente.mongo.obtener_por_id(doc_id.strip())
                    if exito:
                        st.json(doc)
                    else:
                        st.warning(doc)
        with col_b:
            if st.button("Eliminar"):
                if doc_id:
                    exito, msj = agente.mongo.eliminar_medicion(doc_id.strip())
                    if exito:
                        st.success(msj)
                    else:
                        st.error(msj)

with col_derecha:
    st.subheader("📈 Registros y Gráficas")

    exito, docs = agente.mongo.obtener_todos()
    if exito and docs:
        df = pd.DataFrame(docs)
        df["_id"] = df["_id"].astype(str)

        if "temperatura_c" in df.columns and "humedad_pct" in df.columns:
            st.markdown("#### Gráfica de Temperatura y Humedad")
            df_grafica = df[["temperatura_c", "humedad_pct"]].copy()
            df_grafica.columns = ["Temperatura (°C)", "Humedad (%)"]
            st.line_chart(df_grafica)

        st.markdown("#### Tabla de Datos")
        st.dataframe(df[["_id", "temperatura_c", "humedad_pct", "accion"]], use_container_width=True)
    else:
        st.info("No hay registros guardados en MongoDB Atlas.")