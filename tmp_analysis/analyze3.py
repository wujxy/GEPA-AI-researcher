import numpy as np
data = np.loadtxt("examples/function_discovery/data/observations.csv", skiprows=1)
from scipy import stats
n = len(data)
mu, sd = stats.norm.fit(data)
ll_norm = np.sum(stats.norm.logpdf(data, mu, sd))
print("normal ll=", ll_norm, "AIC=", 2*2-2*ll_norm, "BIC=", 2*np.log(n)-2*ll_norm)
ks = stats.kstest(data, "norm", args=(mu,sd))
print("KS normal stat=", ks.statistic, "p=", ks.pvalue)
sh = stats.shapiro(data)
print("Shapiro=", sh)
da = stats.normaltest(data)
print("DA normaltest=", da)
