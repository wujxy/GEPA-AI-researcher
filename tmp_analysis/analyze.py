
import numpy as np
data = np.loadtxt("examples/function_discovery/data/observations.csv", skiprows=1)
print("n =", len(data))
print("min =", data.min(), "max =", data.max())
print("mean =", data.mean(), "std =", data.std())
print("median =", np.median(data))
print("skew =", np.mean(((data-data.mean())/data.std())**3))
print("kurt =", np.mean(((data-data.mean())/data.std())**4) - 3)
print("---percentiles---")
for p in [1,5,10,25,50,75,90,95,99]:
    print(p, np.percentile(data,p))
from scipy import stats
print("---normal fit---", stats.norm.fit(data))
print("---gamma fit---", stats.gamma.fit(data))
print("---KS normal---", stats.kstest(data, "norm", args=stats.norm.fit(data)))
print("---KS gamma---", stats.kstest(data, "gamma", args=stats.gamma.fit(data)))
print("---KS expon---", stats.kstest(data, "expon", args=stats.expon.fit(data)))
from sklearn.mixture import GaussianMixture
for k in range(1,6):
    gmm = GaussianMixture(n_components=k).fit(data.reshape(-1,1))
    print("GMM k=", k, "bic=", gmm.bic(data.reshape(-1,1)), "aic=", gmm.aic(data.reshape(-1,1)))
