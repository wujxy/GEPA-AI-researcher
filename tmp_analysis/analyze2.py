import numpy as np
data = np.loadtxt("examples/function_discovery/data/observations.csv", skiprows=1)
from scipy import stats
for name, dist in [("logistic", stats.logistic), ("t", stats.t), ("skewnorm", stats.skewnorm), ("laplace", stats.laplace), ("gumbel_r", stats.gumbel_r), ("gumbel_l", stats.gumbel_l), ("cauchy", stats.cauchy), ("beta", stats.beta)]:
    try:
        params = dist.fit(data)
        ks = stats.kstest(data, dist.name, args=params)
        ll = np.sum(dist.logpdf(data, *params))
        print(name, "params=", [round(float(p),3) for p in params], "KS=", round(float(ks.statistic),4), "p=", round(float(ks.pvalue),4), "ll=", round(float(ll),2))
    except Exception as e:
        print(name, "ERR", str(e)[:50])
