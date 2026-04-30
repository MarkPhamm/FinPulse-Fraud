"""Smoke job: read a text file from HDFS, count words, print a sentinel line.

The smoke script greps stdout for `SMOKE_OK total_words=6 distinct=3` to confirm
that the Spark cluster can talk to HDFS via the mounted hadoop-client config.
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import explode, split


def main() -> None:
    spark = (
        SparkSession.builder.appName("finpulse-smoke")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.text("hdfs://namenode:9000/smoke/words.txt")
    words = df.select(explode(split(df.value, r"\s+")).alias("w")).where("w != ''")

    total = words.count()
    distinct = words.distinct().count()

    print(f"SMOKE_OK total_words={total} distinct={distinct}")
    spark.stop()


if __name__ == "__main__":
    main()
