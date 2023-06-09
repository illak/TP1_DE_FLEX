import os
import requests
import json
import pandas as pd

import pyspark
import psycopg2

from pyspark.sql import DataFrame, SparkSession
from typing import List
import pyspark.sql.types as T
import pyspark.sql.functions as F

from functools import reduce

# Example from https://pypi.org/project/python-dotenv/
from dotenv import load_dotenv
load_dotenv()

# OR, the same with increased verbosity
load_dotenv(verbose=True)

# Cargamos PYSPARK ====================================================================

spark= SparkSession \
       .builder \
       .config("spark.jars", "jars/postgresql-42.2.27.jre7.jar") \
       .appName("Spark - ETL ApiNews") \
       .getOrCreate()


# Función para obtener páginas de articulos ====================================================================

def generateDataframeApiWeather(topic, pages, language = 'en'):

    df_list = []
    pages_temp = 0

    # first query to get number of results and decide number of pages
    query =  {  'q': topic,
                'language': language,
                'apiKey': os.environ.get('NEWSAPI_KEY')}

    response = requests.get('https://newsapi.org/v2/everything', params=query)

    #convert string to  object
    json_object = json.loads(response.content.decode('utf-8'))
    print("La API encontró {num} artículos sobre el tema consultado".format(num = json_object['totalResults']))

    if(json_object['totalResults'] == 0):
        raise Exception("No se encontraron resultados sobre el tema consultado! :( ")

    elif(json_object['totalResults'] <= 100):
        print("Se ignora el parámetro de cantidad de páginas...")

        df = spark.createDataFrame(data=json_object['articles']) \
            .withColumn("source_name", F.col("source").getItem("name")).drop(F.col("source")) \
            .withColumn("publishedAt", F.to_timestamp("publishedAt")) \
            #.withColumn("idrecord", F.lit(0))

        df_list.append(df)
        return df_list

    elif(json_object['totalResults'] > 100):
        
        # Agregamos los primeros 100 artículos
        df = spark.createDataFrame(data=json_object['articles']) \
                .withColumn("source_name", F.col("source").getItem("name")) \
                .withColumn("source_id", F.col("source").getItem("id")) \
                .drop(F.col("source")) \
                .withColumn("publishedAt", F.to_timestamp("publishedAt"))

        df_list.append(df)

        pages_temp = int(json_object['totalResults']/100) + (0 if(json_object['totalResults']%100==0) else 1)

        print("Se encontraron {pages_total} páginas en total... obteniendo artículos para {pags} páginas...".format(pags = pages, pages_total = pages_temp))
        # Corrección de num de páginas solicitadas (depende de la cantidad de resultados obtenidos)
        if (pages_temp < pages):
            pages = pages_temp


        for p in range(2, pages+1):
            query = {'q': topic,
                    'page': p+1,
                    'language': language,
                    'apiKey': os.environ.get('NEWSAPI_KEY')}

            response = requests.get('https://newsapi.org/v2/everything', params=query)

            #convert string to  object
            json_object = json.loads(response.content.decode('utf-8'))

            df = spark.createDataFrame(data=json_object['articles']) \
                    .withColumn("source_name", F.col("source").getItem("name")) \
                    .withColumn("source_id", F.col("source").getItem("id")) \
                    .drop(F.col("source")) \
                    .withColumn("publishedAt", F.to_timestamp("publishedAt"))

            df_list.append(df)

        return df_list
    else:
        raise("Ocurrió un error inesperado!")
    


# Función que realiza la carga a Redshift
def realizarCarga(df, url, properties, tabla, append=False):
    # Escribimos los datos del dataframe a la BD en redshift

    # Usamos "time" para controlar el tiempo de carga a la BD
    import time

    # Arrancamos "cronómetro"
    st = time.time()

    # usamos modo "append" para agregar los datos a los ya existentes
    if append:
        df.write.jdbc(
            url,
            f"i_zapata1989_coderhouse.{tabla}",
            mode="append",
            properties=properties)
    else:
        df.write \
            .option("truncate", "true") \
            .option("createTableColumnTypes", "author VARCHAR(1000), content VARCHAR(1000), description VARCHAR(1000), publishedat timestamp, title VARCHAR(1000), url VARCHAR(1000), urltoimage VARCHAR(1000)") \
            .jdbc(
                url,
                f"i_zapata1989_coderhouse.{tabla}",
                mode="overwrite",
                properties=properties
            )

    # Detenemos "cronómetro"
    et = time.time()

    elapsed_time = et - st
    print('Tiempo de ejecución tarea de ecsritura en la BD:', elapsed_time, 'segundos')


def limpiarTablaRemota(table, schema, conf):
    print('Starting connection...')
    conn = create_conn(config=conf)
    cursor = conn.cursor()
    print('Starting DELETE FROM ...')

    # Limpiamos los datos
    cursor.execute(f"delete from {schema}.{table};")

    # Es necesario hacer un COMMIT para que se vea reflejado en la DB
    print('Commiting ...')
    conn.commit()

    print('Finishing DELETE FROM ...')

    print('Closing cursor...')
    cursor.close()
    print('Closing connection...')
    conn.close()

    
# función que crea la conexión con redshift
def create_conn(*args, **kwargs):

    config = kwargs['config']
    try:
        conn=psycopg2.connect(dbname = config['dbname'],
                            host = config['host'],
                            port = config['port'],
                            user = config['user'],
                            password = config['password'])
    except Exception as err:
        print(err)

    return conn


# Función que elimina registros de una tabla en Redshift
def limpiarTablaRemota(table, schema, conf):

    print('Starting connection...')

    conf = {'dbname' : os.environ.get('REDSHIFT_DATABASE'),
            'host' : os.environ.get('REDSHIFT_HOST'),
            'port': os.environ.get('REDSHIFT_PORT'),
            'user': os.environ.get('REDSHIFT_USER'),
            'password': os.environ.get('REDSHIFT_PASS')}
    
    conn = create_conn(config=conf)
    cursor = conn.cursor()
    print('Starting DELETE FROM ...')

    # Limpiamos los datos
    cursor.execute(f"delete from {schema}.{table};")

    # Es necesario hacer un COMMIT para que se vea reflejado en la DB
    print('Commiting ...')
    conn.commit()

    print('Finishing DELETE FROM ...')

    print('Closing cursor...')
    cursor.close()
    print('Closing connection...')
    conn.close()


#=============================
# ETAPA "EXTRACT"
#=============================

# ====================================================================
def extract():
    try:
        df_articles = generateDataframeApiWeather('data engineer', 2)
        df_complete = reduce(DataFrame.unionAll, df_articles)

        df_complete.printSchema()
        df_complete.show()

        #df_serial = df_complete.toPandas().to_json(orient="records")
        #df_serial = json.loads(df_serial)

        url = "jdbc:postgresql://{host}:{port}/{database}".format(
            host=os.environ.get('REDSHIFT_HOST'),
            port=os.environ.get('REDSHIFT_PORT'),
            database=os.environ.get('REDSHIFT_DATABASE'))

        properties = {
            "user": os.environ.get('REDSHIFT_USER'),
            "password": os.environ.get('REDSHIFT_PASS'),
            "driver": "org.postgresql.Driver"
        }

        realizarCarga(df_complete, url, properties, "df_news_api_dw_temp")
        #return df_serial
    except Exception as e:
        print(e)


#=============================
# ETAPA "TRANSFORM" + ETAPA "LOAD"
#=============================

def transform_load():

    conf = {
        'dbname' : os.environ.get('REDSHIFT_DATABASE'),
        'host' : os.environ.get('REDSHIFT_HOST'),
        'port': os.environ.get('REDSHIFT_PORT'),
        'user': os.environ.get('REDSHIFT_USER'),
        'password': os.environ.get('REDSHIFT_PASS')}

    url = "jdbc:postgresql://{host}:{port}/{database}".format(
        host=os.environ.get('REDSHIFT_HOST'),
        port=os.environ.get('REDSHIFT_PORT'),
        database=os.environ.get('REDSHIFT_DATABASE'))

    properties = {
        "user": os.environ.get('REDSHIFT_USER'),
        "password": os.environ.get('REDSHIFT_PASS'),
        "driver": "org.postgresql.Driver"
    }

    print("="*30)
    # Read table using jdbc()
    df_complete = spark.read \
        .jdbc(url, "df_news_api_dw_temp", properties=properties)
    
    df_complete.printSchema()
            
    # Luego de consumir los datos, limpiamos tabla temporal
    limpiarTablaRemota("df_news_api_dw_temp", "i_zapata1989_coderhouse", conf)

    print("="*30)

    # quitamos registros duplicados
    print("Removiendo registros duplicados ...")
    df_complete_dedup = df_complete.distinct()

    # chequeamos si se quitaron duplicados
    print(f"Tamaño luego de eliminar registros duplicados: {df_complete_dedup.count()} registros")

    # Eliminamos registros que tengan valores nulos en las columnas especificadas
    print("Filtrando filas con valores nulos ...")
    df_complete_no_nulls = df_complete_dedup.dropna(subset=["author","description"])
    
    realizarCarga(df_complete_no_nulls, url, properties, "df_news_api_dw", append=True)



if __name__ == "__main__":
    extract()
    transform_load()