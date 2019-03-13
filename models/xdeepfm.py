import tensorflow as tf
from src import misc_utils as utils
from tensorflow.python.ops import lookup_ops
from tensorflow.python.layers import core as layers_core
from models.base_model import BaseModel
import numpy as np
import time 
import os
class Model(BaseModel):
    def __init__(self,hparams):
        self.hparams=hparams
        if hparams.metric in ['logloss']:
            self.best_score=100000
        else:
            self.best_score=0
        self.build_graph(hparams)   
        self.optimizer(hparams)
        params = tf.trainable_variables()
        utils.print_out("# Trainable variables")
        for param in params:
            utils.print_out("  %s, %s, %s" % (param.name, str(param.get_shape()),param.op.device))   
  
    def set_Session(self,sess):
        self.sess=sess
        
    def build_graph(self, hparams):
        self.initializer = self._get_initializer(hparams)
        self.label = tf.placeholder(shape=(None), dtype=tf.float32)
        self.use_norm=tf.placeholder(tf.bool)
        self.features=tf.placeholder(shape=(None,hparams.feature_nums), dtype=tf.int32)
        self.emb_v1=tf.get_variable(shape=[hparams.hash_ids,1],
                                    initializer=self.initializer,name='emb_v1')
        self.emb_v2=tf.get_variable(shape=[hparams.hash_ids,hparams.k],
                                    initializer=self.initializer,name='emb_v2')
        
        #lr
        emb_inp_v1=tf.gather(self.emb_v1, self.features)
        lr_logits=tf.reduce_sum(emb_inp_v1,[-1,-2])
        
        emb_inp_v2=tf.gather(self.emb_v2, self.features)
        self.emb_inp_v2=emb_inp_v2
        #DNN
        dnn_input=tf.reshape(emb_inp_v2,[-1,hparams.feature_nums*hparams.k])
        input_size=int(dnn_input.shape[-1])
        for idx in range(len(hparams.hidden_size)):
            glorot = np.sqrt(2.0 / (input_size + hparams.hidden_size[idx]))
            W = tf.Variable(np.random.normal(loc=0, scale=glorot, size=(input_size, hparams.hidden_size[idx])), dtype=np.float32)
            dnn_input=tf.tensordot(dnn_input,W,[[-1],[0]])
            if hparams.norm is True:
                dnn_input=self.batch_norm_layer(dnn_input,\
                                           self.use_norm,'norm_'+str(idx))
            dnn_input=tf.nn.relu(dnn_input)
            input_size=hparams.hidden_size[idx]

        glorot = np.sqrt(2.0 / (hparams.hidden_size[-1] + 1))
        W = tf.Variable(np.random.normal(loc=0, scale=glorot, size=(hparams.hidden_size[-1], 1)), dtype=np.float32)     
        dnn_logits=tf.tensordot(dnn_input,W,[[-1],[0]])[:,0]
        
        #exFM
        exfm_logit=self._build_extreme_FM(hparams, emb_inp_v2, res=False, direct=False, bias=False, reduce_D=False, f_dim=2)[:,0]       

        logit=lr_logits+dnn_logits+exfm_logit
        self.prob=tf.sigmoid(logit)
        logit_1=tf.log(self.prob+1e-20)
        logit_0=tf.log(1-self.prob+1e-20)
        self.loss=-tf.reduce_mean(self.label*logit_1+(1-self.label)*logit_0)
        self.cost=-(self.label*logit_1+(1-self.label)*logit_0)
        self.saver= tf.train.Saver()
        
    def _build_extreme_FM(self, hparams, nn_input, res=False, direct=False, bias=False, reduce_D=False, f_dim=2):
        hidden_nn_layers = []
        field_nums = []
        final_len = 0
        field_num = hparams.feature_nums
        nn_input = tf.reshape(nn_input, shape=[-1, int(field_num), hparams.k])
        field_nums.append(int(field_num))
        hidden_nn_layers.append(nn_input)
        final_result = []
        split_tensor0 = tf.split(hidden_nn_layers[0], hparams.k * [1], 2)
        with tf.variable_scope("exfm_part", initializer=self.initializer) as scope:
            for idx, layer_size in enumerate(hparams.cross_layer_sizes):
                split_tensor = tf.split(hidden_nn_layers[-1], hparams.k * [1], 2)
                dot_result_m = tf.matmul(split_tensor0, split_tensor, transpose_b=True)
                dot_result_o = tf.reshape(dot_result_m, shape=[hparams.k, -1, field_nums[0]*field_nums[-1]])
                dot_result = tf.transpose(dot_result_o, perm=[1, 0, 2])

                if reduce_D:
                    filters0 = tf.get_variable("f0_" + str(idx),
                                               shape=[1, layer_size, field_nums[0], f_dim],
                                               dtype=tf.float32)
                    filters_ = tf.get_variable("f__" + str(idx),
                                               shape=[1, layer_size, f_dim, field_nums[-1]],
                                               dtype=tf.float32)
                    filters_m = tf.matmul(filters0, filters_)
                    filters_o = tf.reshape(filters_m, shape=[1, layer_size, field_nums[0] * field_nums[-1]])
                    filters = tf.transpose(filters_o, perm=[0, 2, 1])
                else:
                    filters = tf.get_variable(name="f_"+str(idx),
                                         shape=[1, field_nums[-1]*field_nums[0], layer_size],
                                         dtype=tf.float32)
                # dot_result = tf.transpose(dot_result, perm=[0, 2, 1])
                curr_out = tf.nn.conv1d(dot_result, filters=filters, stride=1, padding='VALID')
                
                # BIAS ADD
                if bias:
                    b = tf.get_variable(name="f_b" + str(idx),
                                    shape=[layer_size],
                                    dtype=tf.float32,
                                    initializer=tf.zeros_initializer())
                    curr_out = tf.nn.bias_add(curr_out, b)

                curr_out = self._activate(curr_out, hparams.cross_activation)
                
                curr_out = tf.transpose(curr_out, perm=[0, 2, 1])
                
                if direct:

                    direct_connect = curr_out
                    next_hidden = curr_out
                    final_len += layer_size
                    field_nums.append(int(layer_size))

                else:
                    if idx != len(hparams.cross_layer_sizes) - 1:
                        next_hidden, direct_connect = tf.split(curr_out, 2 * [int(layer_size / 2)], 1)
                        final_len += int(layer_size / 2)
                    else:
                        direct_connect = curr_out
                        next_hidden = 0
                        final_len += layer_size
                    field_nums.append(int(layer_size / 2))

                final_result.append(direct_connect)
                hidden_nn_layers.append(next_hidden)


            result = tf.concat(final_result, axis=1)
            
            result = tf.reduce_sum(result, -1)
            if res:
                w_nn_output1 = tf.get_variable(name='w_nn_output1',
                                               shape=[final_len, 128],
                                               dtype=tf.float32)
                b_nn_output1 = tf.get_variable(name='b_nn_output1',
                                               shape=[128],
                                               dtype=tf.float32,
                                               initializer=tf.zeros_initializer())
                self.layer_params.append(w_nn_output1)
                self.layer_params.append(b_nn_output1)
                exFM_out0 = tf.nn.xw_plus_b(result, w_nn_output1, b_nn_output1)
                exFM_out1 = self._active_layer(logit=exFM_out0,
                                               scope=scope,
                                               activation="relu",
                                               layer_idx=0)
                w_nn_output2 = tf.get_variable(name='w_nn_output2',
                                               shape=[128 + final_len, 1],
                                               dtype=tf.float32)
                b_nn_output2 = tf.get_variable(name='b_nn_output2',
                                               shape=[1],
                                               dtype=tf.float32,
                                               initializer=tf.zeros_initializer())
                self.layer_params.append(w_nn_output2)
                self.layer_params.append(b_nn_output2)
                exFM_in = tf.concat([exFM_out1, result], axis=1, name="user_emb")
                exFM_out = tf.nn.xw_plus_b(exFM_in, w_nn_output2, b_nn_output2)

            else:
                w_nn_output = tf.get_variable(name='w_nn_output',
                                              shape=[final_len, 1],
                                              dtype=tf.float32)
                b_nn_output = tf.get_variable(name='b_nn_output',
                                              shape=[1],
                                              dtype=tf.float32,
                                              initializer=tf.zeros_initializer())
                exFM_out = tf.nn.xw_plus_b(result, w_nn_output, b_nn_output)

            return exFM_out
        
 
    def optimizer(self,hparams):
        opt=self._build_train_opt(hparams)
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss,params,colocate_gradients_with_ops=True)
        clipped_grads, gradient_norm = tf.clip_by_global_norm(gradients, 5.0)  
        self.grad_norm =gradient_norm 
        self.update = opt.apply_gradients(zip(clipped_grads, params)) 

    def train(self,train_data,dev_data):
        hparams=self.hparams
        sess=self.sess
        assert len(train_data[0])==len(train_data[1]), "Size of features data must be equal to label"
        for epoch in range(hparams.epoch):
            info={}
            info['loss']=[]
            info['norm']=[]
            start_time = time.time()
            for idx in range(len(train_data[0])//hparams.batch_size+3):
                try:
                    if hparams.steps<=idx:
                        T=(time.time()-start_time)
                        self.eval(T,dev_data,hparams,sess)
                        break               
                except:
                    pass                
                if idx*hparams.batch_size>=len(train_data[0]):
                    T=(time.time()-start_time)
                    self.eval(T,dev_data,hparams,sess)
                    break
                   
                batch=train_data[0][idx*hparams.batch_size:\
                                    min((idx+1)*hparams.batch_size,len(train_data[0]))]
                batch=utils.hash_batch(batch,hparams)
                label=train_data[1][idx*hparams.batch_size:\
                                    min((idx+1)*hparams.batch_size,len(train_data[1]))]
                loss,_,norm=sess.run([self.loss,self.update,self.grad_norm],feed_dict=\
                                     {self.features:batch,self.label:label,self.use_norm:True})
                info['loss'].append(loss)
                info['norm'].append(norm)
                if (idx+1)%hparams.num_display_steps==0:
                    info['learning_rate']=hparams.learning_rate
                    info["train_ppl"]= np.mean(info['loss'])
                    info["avg_grad_norm"]=np.mean(info['norm'])
                    utils.print_step_info("  ", epoch,idx+1, info)
                    del info
                    info={}
                    info['loss']=[]
                    info['norm']=[]
                if (idx+1)%hparams.num_eval_steps==0 and dev_data:
                    T=(time.time()-start_time)
                    self.eval(T,dev_data,hparams,sess)
        self.saver.restore(sess,'model_tmp/model')
        T=(time.time()-start_time)
        self.eval(T,dev_data,hparams,sess)
        os.system("rm -r model_tmp")
        
      
    def infer(self,dev_data):
        hparams=self.hparams
        sess=self.sess
        assert len(dev_data[0])==len(dev_data[1]), "Size of features data must be equal to label"       
        preds=[]
        total_loss=[]
        for idx in range(len(dev_data[0])//hparams.batch_size+1):
            batch=dev_data[0][idx*hparams.batch_size:\
                              min((idx+1)*hparams.batch_size,len(dev_data[0]))]
            if len(batch)==0:
                break
            batch=utils.hash_batch(batch,hparams)
            label=dev_data[1][idx*hparams.batch_size:\
                              min((idx+1)*hparams.batch_size,len(dev_data[1]))]
            pred=sess.run(self.prob,feed_dict=\
                          {self.features:batch,self.label:label,self.use_norm:False})  
            preds.append(pred)   
        preds=np.concatenate(preds)
        return preds
    def get_embedding(self,dev_data):
        hparams=self.hparams
        sess=self.sess
        assert len(dev_data[0])==len(dev_data[1]), "Size of features data must be equal to label"       
        embedding=[]
        total_loss=[]
        for idx in range(len(dev_data[0])//hparams.batch_size+1):
            batch=dev_data[0][idx*hparams.batch_size:\
                              min((idx+1)*hparams.batch_size,len(dev_data[0]))]
            if len(batch)==0:
                break
            batch=utils.hash_batch(batch,hparams)
            label=dev_data[1][idx*hparams.batch_size:\
                              min((idx+1)*hparams.batch_size,len(dev_data[1]))]
            temp=sess.run(self.emb_inp_v2,\
                          feed_dict={self.features:batch,self.label:label})  
            embedding.append(temp)   
        embedding=np.concatenate(embedding,0)
        return embedding
            