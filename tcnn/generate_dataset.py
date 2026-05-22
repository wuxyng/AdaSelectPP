import random
import time
import sys
sys.path.append("")
import psycopg2
from database.database_connector import DatabaseConnector
from database.generate_workload import generate_tpch_workload, generate_job_workload


def change_configuration(connector, column_to_table, columns, max_num):
    new_indexes = set()
    selected_columns = random.sample(columns, random.randint(1, max_num))
    for column in selected_columns:
        new_indexes.add((column_to_table[column], column))

    existing_indexes = set(connector.get_indexes())
    for index in existing_indexes - new_indexes:
        connector.drop_index(index[0], index[1:])
    for index in new_indexes - existing_indexes:
        connector.create_index(index[0], index[1:])


if __name__ == "__main__":
    print(f"Generating the dataset start: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}")

    # get settings from command parameters
    benchmark = sys.argv[1]  ## tpch or tpchs or job
    workload_num = int(sys.argv[2])
    change_freq = int(sys.argv[3])
    max_indexes = int(sys.argv[4])
    run_num = int(sys.argv[5])
    print(f"Benchmark: {benchmark}, Number of workloads: {workload_num}, Change frequency: {change_freq}"
        f", Maximum number of indexes: {max_indexes}, Number of execution: {run_num}")

    # check whether the benchmark parameter is acceptable
    if benchmark == "tpch" or benchmark == "tpchs":
        generate_algorithm = generate_tpch_workload
    elif benchmark == "job":
        generate_algorithm = generate_job_workload
    else:
        print("Benchmark parameter Error!")
        exit()

    # get all indexable columns and related tables from the txt file
    columns = []  # all indexable columns
    column_to_table = dict()  # the table in which a column is located
    with open(f"txt/{benchmark}_indexable_columns.txt", "r") as f:
        for line in f.readlines():
            line = line.strip().split(" ")
            columns.append(line[0])
            column_to_table[line[0]] = line[1]

    total_change_time = 0.0  # time consumption of changing the configuration
    total_run_time = 0.0  # time consumption of running queries

    file = open(f"tcnn/dataset/{benchmark}_{workload_num}_{change_freq}_"
                f"{max_indexes}_{run_num}.txt", "w")
    db_connector = DatabaseConnector(benchmark, virtual=False, run_num=run_num)

    for workload_id in range(workload_num):
        # change the present configuration
        if workload_id % change_freq == 0:
            print(workload_id)
            change_start_time = time.time()
            change_configuration(db_connector, column_to_table, columns, max_indexes)
            total_change_time += time.time() - change_start_time
        # generate a new workload
        workload = generate_algorithm()
        for query in workload:
            # get the query plan
            plan = db_connector.get_plan(query)
            if plan["Total Cost"] > 250000:
                continue
            # get the query's runtime
            run_start = time.time()
            try:
                query_runtime = db_connector.get_query_runtime(query)
            except psycopg2.errors.QueryCanceled:
                db_connector.rollback()
                continue
            total_run_time += time.time() - run_start
            file.write(f"{plan}\t{query_runtime}\n")

    db_connector.close()
    file.close()
    print(f"Total change time: {total_change_time}s")
    print(f"Total run time: {total_run_time}s")
    print(f"Generating the dataset end: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}")
