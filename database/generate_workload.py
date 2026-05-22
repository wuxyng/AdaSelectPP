import subprocess
import sys
import random
import time


# generate a workload of size 21
def generate_tpch_workload():
    p = subprocess.Popen("./qgen", cwd="database/tpch/dbgen/",
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with p.stdout:
        result = p.stdout.read().decode("utf-8")
    p.wait()
    result = result.split("RNG")[1]

    workload0 = []
    query = ""
    for string in result.split("\n"):
        string = string.strip()
        if string == "go":
            workload0.append(query.strip())
            query = ""
        else:
            query += f"{string} "

    workload1 = []
    for query in workload0:
        if "revenue0" in query:
            continue
        query = query.replace("(3)", "")
        query = query.replace(" set rowcount -1", "")
        query = query.replace("; set rowcount", " limit")
        if query[-1:] != ";":
            query += ";"
        workload1.append(query)
    return workload1


# generate a workload of size 33
def generate_job_workload():
    templates = []
    with open("database/job_workload/templates.txt", "r") as f:
        lines = f.readlines()
        for line in lines:
            templates.append(line.strip())

    values = dict()
    with open("database/job_workload/values.txt", "r") as f:
        lines = f.readlines()
        for i, line in enumerate(lines):
            values[i + 1] = eval(line)

    workload = []
    for query in templates:
        while query.find('$') != -1:
            index = query.find('$')
            variable = query[index:index + 2]
            value = random.choice(values[int(variable[1])])
            query = query.replace(variable, value)
        workload.append(query)
    return workload


def add_a_round_into_workload(workload, templates, generate_func, benchmark):
    mini_workload = generate_func()
    # avoid generating same workloads
    if benchmark == "tpch" or benchmark == "tpchs":
        time.sleep(1)
    for template in templates:
        workload.append(f"{mini_workload[template]}\t{template}\n")


def output_workload(workload, filename):
    with open(filename, "w") as f:
        for query in workload:
            f.write(f"{query}")


if __name__ == "__main__":
    benchmark = sys.argv[1]  # tpch or tpchs or job

    # choose the suitable function to generate workloads
    generate_func = None
    if benchmark == "tpch" or benchmark == "tpchs":
        generate_func = generate_tpch_workload
    elif benchmark == "job":
        generate_func = generate_job_workload
    else:
        print("Benchmark parameter error!")
        exit()
    
    # get templates in a group
    all_num_templates = len(generate_func())
    group_num_templates = int(all_num_templates / 4)
    selected_templates = random.sample(
        list(range(all_num_templates)), 4 * group_num_templates)
    
    # generate the shifting workload
    # All query templates are randomly divided into 4 equal-sized groups
    # A group of query templates is executed for 20 rounds
    # A round contains a query instance with each template
    shifting_workload = []
    for round in range(80):
        i = int(round / 20)  # decide which templates are in this round
        templates = selected_templates[i * group_num_templates: (i + 1) * group_num_templates]
        add_a_round_into_workload(shifting_workload, templates, generate_func, benchmark)
    output_workload(shifting_workload, f"{benchmark}_shifting.txt")
    

    # generate the noisy workload
    # After 20 rounds, 4 noisy rounds are added into the workload
    noisy_workload = []
    for i in range(4):
        noisy_workload.extend(
            shifting_workload[i * 20 * group_num_templates: (i + 1) * 20 * group_num_templates])
        # add 4 rounds as noise
        for j in range(4):
            noisy_i = random.randint(0, 3)
            while noisy_i == i:
                noisy_i = random.randint(0, 3)
            templates = selected_templates[
                noisy_i * group_num_templates: (noisy_i + 1) * group_num_templates]
            add_a_round_into_workload(noisy_workload, templates, generate_func, benchmark)
    output_workload(noisy_workload, f"{benchmark}_noisy.txt")


    # generate the random workload
    # A random workload contains 25 rounds
    # The number of queries in a round is same as the number of templates
    random_workload = []
    for round in range(25):
        mini_workload = generate_func()
        for i, query in enumerate(mini_workload):
            random_workload.append(f"{query}\t{i}\n")
        if benchmark == "tpch" or benchmark == "tpchs":
            time.sleep(1)
    random.shuffle(random_workload)
    output_workload(random_workload, f"{benchmark}_random.txt")
