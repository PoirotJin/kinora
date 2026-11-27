"""Isolated ablation: increase only source leg-direction tracking weight."""
import cvxpy as cp

def install(cls, weight=50.):
    original=cls.pose_objective_callback
    def objective(self,q,dqa,jac,points,frame):
        terms=original(self,q,dqa,jac,points,frame)
        for a,b,length,direction in self._motionfarm_segments[:4]:
            residual=(points[b]-points[a]+(jac[b]-jac[a])@dqa)/length-direction[frame]
            terms.append((weight-2.)*cp.sum_squares(residual))
        return terms
    cls.pose_objective_callback=objective
