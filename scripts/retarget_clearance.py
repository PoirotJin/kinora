"""Robot-specific non-adjacent collision pairs and full-trajectory acceptance."""
VERSION='humanoid_clearance_v1'
TARGET_CLEARANCE_M=0.02
SOLVER_CLEARANCE_M=0.01
MIN_EXPORT_CLEARANCE_M=0.005
REFINEMENT_CLEARANCE_M=0.0075

def configure_collision_backend(model):
    import mujoco
    # MuJoCo 3.4 native CCD reports spurious exact zeros for some Humanoid hulls
    # while its closest points are centimetres apart. Use libccd consistently.
    model.opt.disableflags |= int(mujoco.mjtDisableBit.mjDSBL_NATIVECCD)

def install_collision_backend(cls):
    original=cls.__init__
    def initialize(self,*args,**kwargs):
        original(self,*args,**kwargs)
        configure_collision_backend(self.robot_model)
    cls.__init__=initialize

def install_normal_fallback(cls):
    """Escape coincident closest points instead of adding impossible 0 >= margin."""
    import numpy as np
    original=cls._compute_jacobian_for_contact_relative
    def relative(self,geom1,geom2,name1,name2,fromto,dist):
        if abs(dist)<1e-9 and 'ground' not in (name1+' '+name2).lower():
            p1=self.robot_data.geom_xpos[geom1.id].copy()
            p2=self.robot_data.geom_xpos[geom2.id].copy()
            delta=p1-p2;norm=np.linalg.norm(delta)
            if norm>1e-8:
                ja=self._calc_contact_jacobian_from_point(geom1.bodyid,p1,input_world=True)
                jb=self._calc_contact_jacobian_from_point(geom2.bodyid,p2,input_world=True)
                return (delta/norm)@(ja-jb)
        return original(self,geom1,geom2,name1,name2,fromto,dist)
    cls._compute_jacobian_for_contact_relative=relative
    # MuJoCo returns exactly distmax for pairs beyond the query radius. The
    # upstream <= comparison computes two full Jacobians for every far pair.
    def self_constraints(self,frame_idx):
        import mujoco
        if not self._self_collision_enabled:return {},{}
        if self._self_collision_windows is not None and not any(a<=frame_idx<=b for a,b in self._self_collision_windows):return {},{}
        js={};ds={};radius=float(self.collision_detection_threshold)
        for a,b in self._self_collision_geom_pairs:
            segment=np.zeros(6)
            distance=mujoco.mj_geomDistance(self.robot_model,self.robot_data,a,b,radius,segment)
            if distance>=radius-1e-10:continue
            key=('self',a,b)
            js[key]=self._compute_jacobian_for_contact_relative(self.robot_model.geom(a),self.robot_model.geom(b),self._geom_names[a],self._geom_names[b],segment,distance)
            ds[key]=float(distance)
        self._motionfarm_clearance_linearization=(js,ds)
        return js,ds
    cls._compute_self_collision_constraints=self_constraints

def install_clearance_objective(cls,separate_tracks=False):
    import cvxpy as cp
    original=cls.pose_objective_callback
    def objective(self,q,dqa,jac,points,frame):
        terms=original(self,q,dqa,jac,points,frame)
        if separate_tracks:
            from scipy.spatial.transform import Rotation
            lateral=Rotation.from_quat(q[3:7],scalar_first=True).as_matrix()[:,1]
            gap=lateral@(points['L_Ankle']-points['R_Ankle']+(jac['L_Ankle']-jac['R_Ankle'])@dqa)
            terms.append(300.0*cp.square(cp.pos(.20-gap)))
        js,ds=getattr(self,'_motionfarm_clearance_linearization',({},{}))
        for key,distance in ds.items():
            if distance<TARGET_CLEARANCE_M+0.01:
                linear=distance+js[key][self.q_a_indices]@dqa
                terms.append(2000.0*cp.square(cp.pos(TARGET_CLEARANCE_M-linear)))
        return terms
    cls.pose_objective_callback=objective

def install_clearance_refinement(cls):
    """Do not accept a frame before nonlinear distances satisfy linearised goals."""
    import mujoco
    original=cls.iterate
    def iterate(self,**kwargs):
        q,cost=original(self,**kwargs)
        for _ in range(3):
            self.robot_data.qpos[:]=q;mujoco.mj_forward(self.robot_model,self.robot_data)
            distances=[mujoco.mj_geomDistance(self.robot_model,self.robot_data,a,b,.1,None) for a,b in self._self_collision_geom_pairs]
            if not distances or min(distances)>=REFINEMENT_CLEARANCE_M:return q,cost
            kwargs=dict(kwargs,q_n=q,n_iter=10)
            q,cost=original(self,**kwargs)
        self.robot_data.qpos[:]=q;mujoco.mj_forward(self.robot_model,self.robot_data)
        worst=sorted((float(mujoco.mj_geomDistance(self.robot_model,self.robot_data,a,b,.1,None)),a,b) for a,b in self._self_collision_geom_pairs)[:3]
        details=[(v,self.robot_model.body(int(self.robot_model.geom_bodyid[a])).name,self.robot_model.body(int(self.robot_model.geom_bodyid[b])).name) for v,a,b in worst]
        import os
        if os.environ.get('MOTIONFARM_CLEARANCE_DIAGNOSTIC')=='1':
            import numpy as np
            np.save('/tmp/amp70_failed_refinement.npy',q)
        raise ValueError('Nonlinear clearance did not converge; candidate withheld: '+str(details))
    cls.iterate=iterate

def install_bounded_contact_recovery(cls):
    """Allow at most 5 mm contact-anchor tolerance on infeasible SQP steps.

    Collision, floor, joint and trust-region constraints remain enabled.
    This is a bounded fallback, not removal of stance-foot constraints.
    """
    original=cls.solve_single_iteration
    def solve(self,**kwargs):
        saved=self.foot_sticking_tolerance
        try:
            for tolerance in sorted(set([saved,max(saved,.003),max(saved,.005)])):
                self.foot_sticking_tolerance=tolerance
                try:
                    candidate,cost=original(self,**kwargs)
                    return collision_line_search(self,kwargs,candidate),cost
                except RuntimeError as exc:
                    if 'CVXPY solve failed: infeasible' not in str(exc):raise
                    if tolerance>=max(saved,.005):raise
        finally:self.foot_sticking_tolerance=saved
    cls.solve_single_iteration=solve

def collision_line_search(self,kwargs,candidate):
    """Keep a valid iterate feasible when linear mesh distances overshoot."""
    import mujoco
    import numpy as np
    previous=kwargs['q_locked'].copy()
    previous[self.q_a_indices]=kwargs['q_a_n_last']
    def distance(q):
        self.robot_data.qpos[:]=q;mujoco.mj_forward(self.robot_model,self.robot_data)
        return min(mujoco.mj_geomDistance(self.robot_model,self.robot_data,a,b,.1,None)
                   for a,b in self._self_collision_geom_pairs)
    # Initialisation may start in collision; leave its recovery to SQP/refinement.
    floor=REFINEMENT_CLEARANCE_M
    if distance(previous)<floor or distance(candidate)>=floor:return candidate
    for alpha in (.5,.25,.125,.0625,.03125,.015625):
        trial=previous+alpha*(candidate-previous)
        trial[3:7]/=np.linalg.norm(trial[3:7])
        if distance(trial)>=floor:return trial
    return previous

def body_pairs(model):
    names={model.body(i).name for i in range(1,model.nbody)}
    pairs=set()
    left=[n for n in names if n.startswith('left_leg_') and any(k in n for k in ('knee','ankle'))]
    right=[n for n in names if n.startswith('right_leg_') and any(k in n for k in ('knee','ankle'))]
    pairs.update((a,b) for a in left for b in right)
    trunk={n for n in names if n in ('base_link','waist_yaw_link','head_yaw_link','head_pitch_link') or 'leg_pelvic' in n or 'leg_knee' in n}
    for side,opposite in [('left','right'),('right','left')]:
        arms={n for n in names if n.startswith(side+'_') and (any(k in n for k in ('wrist','thumb','index','middle','ring','pinky')) or n==side+'_elbow_pitch_link')}
        other={n for n in names if n.startswith(opposite+'_') and any(k in n for k in ('shoulder','elbow','wrist'))}
        pairs.update((a,b) for a in arms for b in trunk|other)
    # Canonicalise symmetric pairs; preserve only bodies with collision geometry.
    enabled={model.body(int(model.geom_bodyid[g])).name for g in range(model.ngeom) if model.geom_contype[g] or model.geom_conaffinity[g]}
    result=sorted({tuple(sorted((a,b))) for a,b in pairs if a!=b and a in enabled and b in enabled})
    if not any('left_leg_ankle_roll_link' in p and 'right_leg_ankle_roll_link' in p for p in result):
        raise ValueError('Foot collision geometry is missing')
    if len(result)<20:raise ValueError('Incomplete Humanoid collision body mapping')
    return result

def geom_pairs(model,pairs):
    mapping={}
    for i in range(model.ngeom):
        if model.geom_contype[i] or model.geom_conaffinity[i]:
            mapping.setdefault(model.body(int(model.geom_bodyid[i])).name,[]).append(i)
    return [(ga,gb,a,b) for a,b in pairs for ga in mapping[a] for gb in mapping[b]]

def validate(model,qpos,pairs,minimum=MIN_EXPORT_CLEARANCE_M):
    import mujoco
    import numpy as np
    configure_collision_backend(model)
    data=mujoco.MjData(model)
    geometry=geom_pairs(model,pairs)
    worst={'distance_m':1.0,'frame':None,'bodies':None}
    failed_frames=0;examples=[]
    for frame,pose in enumerate(qpos):
        data.qpos[:]=pose;mujoco.mj_forward(model,data)
        failed=False
        for ga,gb,a,b in geometry:
            distance=float(mujoco.mj_geomDistance(model,data,ga,gb,0.1,None))
            if not np.isfinite(distance):raise ValueError('Non-finite collision distance')
            if distance<worst['distance_m']:worst=dict(distance_m=distance,frame=frame,bodies=[a,b])
            if distance<minimum:
                failed=True
                if len(examples)<10:examples.append(dict(frame=frame,bodies=[a,b],distance_m=distance))
        failed_frames+=failed
    return dict(version=VERSION,collision_backend='libccd',passed=failed_frames==0,frames=len(qpos),failed_frames=failed_frames,
                target_clearance_m=TARGET_CLEARANCE_M,minimum_export_clearance_m=minimum,
                solver_clearance_m=SOLVER_CLEARANCE_M,
                refinement_clearance_m=REFINEMENT_CLEARANCE_M,
                checked_body_pairs=len(pairs),checked_geom_pairs=len(geometry),worst=worst,examples=examples,
                scope='Selected non-adjacent feet/lower-leg and arm/hand vs trunk/hip/leg/opposite-arm collision geoms; MuJoCo convex collision surfaces, not dynamics or all-body certification')
